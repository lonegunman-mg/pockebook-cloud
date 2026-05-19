#!/usr/bin/env python3
import argparse, json, sys, time
from pathlib import Path
from urllib.parse import quote

try:
    import requests
except ImportError:
    print("pip install requests tqdm"); sys.exit(1)
try:
    from tqdm import tqdm
    HAS_TQDM = True
except ImportError:
    HAS_TQDM = False

API_BASE      = "https://cloud.pocketbook.digital"
API_V10       = f"{API_BASE}/api/v1.0"
CLIENT_ID     = "qNAx1RDb"
CLIENT_SECRET = "K3YYSjCgDJNoWKdGVOyO1mrROp3MMZqqRNXNXTmh"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
    "Accept":     "application/json",
    "Origin":     API_BASE,
    "Referer":    f"{API_BASE}/browser/en",
}

def auth_headers(token):
    return {**HEADERS, "Authorization": f"Bearer {token}"}

def get_providers(session, username, debug=False):
    r = session.get(f"{API_V10}/auth/login",
        params={"username": username, "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET, "language": "en"},
        headers=HEADERS, timeout=30)
    r.raise_for_status()
    data = r.json()
    if debug: print(f"[DEBUG] Provider: {json.dumps(data, indent=2)}")
    providers = data.get("providers", [])
    if not providers:
        print("[!] Nessun provider trovato."); sys.exit(1)
    return providers

def login_with_provider(session, username, password, provider, debug=False):
    alias   = provider["alias"]
    shop_id = provider.get("shop_id", "1")
    r = session.post(f"{API_V10}/auth/login/{alias}",
        data={"username": username, "password": password,
              "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET,
              "grant_type": "password", "shop_id": shop_id},
        headers={**HEADERS, "Content-Type": "application/x-www-form-urlencoded"},
        timeout=30)
    if debug: print(f"[DEBUG] Login {alias} → {r.status_code}: {r.text[:300]}")
    if r.status_code in (401, 403): return None
    r.raise_for_status()
    data  = r.json()
    token = data.get("access_token") or data.get("token") or data.get("auth_token")
    return (token, data.get("refresh_token"), data.get("expires_in", 3600)) if token else None

def login(session, username, password, debug=False):
    print(f"[*] Recupero provider per {username}...")
    providers = get_providers(session, username, debug)
    print(f"[*] Provider: {[p['alias'] for p in providers]}")
    for provider in providers:
        print(f"[*] Provo: {provider['alias']} ...")
        result = login_with_provider(session, username, password, provider, debug)
        if result:
            print(f"[+] Login OK con: {provider['alias']}")
            return result
    print("[!] Login fallito con tutti i provider. Verifica credenziali.")
    sys.exit(1)

def renew_token(session, token, refresh_tok):
    try:
        r = session.post(f"{API_V10}/auth/renew-token",
            data={"grant_type": "refresh_token", "refresh_token": refresh_tok,
                  "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET},
            headers={**HEADERS, "Content-Type": "application/x-www-form-urlencoded"},
            timeout=30)
        if r.ok:
            d = r.json()
            return d.get("access_token") or d.get("token") or token
    except Exception:
        pass
    return token

def get_books(session, token, debug=False):
    all_books, page = [], 1
    print("[*] Recupero lista libri...")
    while True:
        r = session.get(f"{API_V10}/books", headers=auth_headers(token),
                        params={"page": page, "per_page": 100}, timeout=30)
        r.raise_for_status()
        data = r.json()
        if debug: print(f"[DEBUG] p{page}: {str(data)[:300]}")
        if isinstance(data, list):
            books, has_more, total = data, len(data) == 100, "?"
        else:
            books = data.get("items") or data.get("books") or data.get("data") or []
            total = data.get("total") or data.get("count") or len(books)
            has_more = (len(all_books) + len(books)) < int(total)
        all_books.extend(books)
        print(f"    Pagina {page}: {len(books)} libri (tot: {len(all_books)}/{total})")
        if not books or not has_more: break
        page += 1
    print(f"[+] Totale: {len(all_books)} libri")
    return all_books

def safe_filename(name):
    for ch in r'\/:*?"<>|': name = name.replace(ch, "_")
    return name.strip()[:180]

def download_book(session, token, book, output_dir, skip_existing, debug=False):
    book_id = str(book.get("id") or "")
    title   = book.get("title") or book.get("name") or f"libro_{book_id}"
    author  = book.get("author") or book.get("authors") or ""
    path    = book.get("path") or ""
    mime    = book.get("mime_type") or ""
    if isinstance(author, list):
        author = ", ".join(a.get("name", str(a)) if isinstance(a, dict) else str(a) for a in author)
    ext = Path(path).suffix.lstrip(".").lower() if path else (
          mime.split("/")[-1].replace("x-","").replace("+zip","").lower() if mime else "epub")
    parts    = ([safe_filename(author)] if author else []) + [safe_filename(title)]
    filename = " - ".join(parts) + f".{ext}"
    dest     = output_dir / filename
    if skip_existing and dest.exists() and dest.stat().st_size > 0:
        print("    [=] Skip (già presente)."); return "skipped"
    candidates = []
    if path:
        enc = quote(path, safe="/")
        candidates += [f"{API_V10}/files{enc}", f"{API_V10}/file{enc}",
                       f"{API_V10}/storage{enc}", f"{API_V10}/user/files{enc}"]
    if book_id:
        candidates += [f"{API_V10}/books/{book_id}/download",
                       f"{API_V10}/books/{book_id}/download?format={ext}",
                       f"{API_V10}/books/{book_id}/file"]
    if debug:
        print(f"    [DEBUG] path={path!r} ext={ext}")
        for u in candidates[:2]: print(f"           {u}")
    for url in candidates:
        try:
            resp = session.get(url, headers=auth_headers(token),
                               stream=True, timeout=120, allow_redirects=True)
            if debug: print(f"    [DEBUG] {url} → {resp.status_code}")
            if resp.status_code == 404: continue
            resp.raise_for_status()
            ct = resp.headers.get("Content-Type", "")
            if "json" in ct:
                body = resp.content
                if b'"error' in body:
                    if debug: print(f"    [DEBUG] JSON errore: {body[:100]}")
                    continue
                try:
                    d = json.loads(body)
                    real = d.get("url") or d.get("download_url")
                    if real:
                        resp = session.get(real, headers=auth_headers(token),
                                           stream=True, timeout=120)
                        resp.raise_for_status()
                except Exception: continue
            total_size = int(resp.headers.get("content-length", 0))
            with open(dest, "wb") as f:
                if HAS_TQDM and total_size:
                    with tqdm(total=total_size, unit="B", unit_scale=True,
                              desc=dest.name[-40:], leave=False) as bar:
                        for chunk in resp.iter_content(8192):
                            f.write(chunk); bar.update(len(chunk))
                else:
                    done = 0
                    for chunk in resp.iter_content(8192):
                        f.write(chunk); done += len(chunk)
                        if total_size:
                            print(f"\r    {done/total_size*100:.0f}%", end="", flush=True)
                    if total_size: print()
            size = dest.stat().st_size
            if size < 200:
                raw = dest.read_bytes()
                if b'"error' in raw:
                    if debug: print(f"    [DEBUG] File errore: {raw}")
                    dest.unlink(); continue
            print(f"    [✓] {filename} ({size/1024:.0f} KB)")
            return "ok"
        except Exception as e:
            if debug: print(f"    [DEBUG] Errore: {e}")
            continue
    print(f"    [✗] Impossibile scaricare: {title}")
    return "failed"

def main():
    p = argparse.ArgumentParser(description="PocketBook Cloud Downloader")
    p.add_argument("--username", "-u", required=True)
    p.add_argument("--password", "-p", required=True)
    p.add_argument("--dir",      "-d", default="pocketbook_libri")
    p.add_argument("--skip-existing", action="store_true", default=True)
    p.add_argument("--debug",         action="store_true")
    args = p.parse_args()
    print("[*] PocketBook Cloud Downloader")
    out = Path(args.dir)
    out.mkdir(parents=True, exist_ok=True)
    print(f"[*] Cartella: {out.resolve()}")
    session = requests.Session()
    session.headers.update(HEADERS)
    token, refresh_tok, expires_in = login(session, args.username, args.password, args.debug)
    expiry = time.time() + expires_in - 60
    books = get_books(session, token, args.debug)
    if not books:
        print("[!] Nessun libro trovato."); sys.exit(0)
    if args.debug:
        print(f"\n[DEBUG] Struttura libro #0:\n{json.dumps(books[0], indent=2, ensure_ascii=False)}\n")
    ok = skipped = failed = 0
    for i, book in enumerate(books, 1):
        if refresh_tok and time.time() > expiry:
            token = renew_token(session, token, refresh_tok)
            expiry = time.time() + 3540
            print("[*] Token rinnovato.")
        title  = book.get("title") or f"libro_{i}"
        author = book.get("author") or ""
        print(f"\n[{i}/{len(books)}] {title}" + (f" — {author}" if author else ""))
        result = download_book(session, token, book, out, args.skip_existing, args.debug)
        if result == "ok":        ok += 1
        elif result == "skipped": skipped += 1
        else:                     failed += 1; time.sleep(1)
    print(f"""
╔══════════════════════════════════╗
  ✓ Scaricati:  {ok}
  = Saltati:    {skipped}
  ✗ Falliti:    {failed}
  📁 {out.resolve()}
╚══════════════════════════════════╝""")

if __name__ == "__main__":
    main()
