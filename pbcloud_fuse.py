#!/usr/bin/env python3
"""
PocketBook Cloud FUSE mount — v3
Monta la libreria PocketBook Cloud come filesystem locale (sola lettura).

Dipendenze:
  sudo apt install python3-fusepy
  pip install requests   (oppure sudo apt install python3-requests)

Utilizzo:
  python3 pbcloud_fuse.py --email TUA@EMAIL --password TUAPASSWORD --mountpoint ./pocketbook_libri [--foreground] [--debug]

Smonta:
  fusermount -u ./pocketbook_libri
"""

import os
import sys
import errno
import time
import json
import argparse
import logging
from pathlib import Path
from urllib.parse import quote

try:
    import requests
except ImportError:
    print("Installa requests: pip install requests"); sys.exit(1)

try:
    from fusepy import FUSE, FuseOSError, Operations, LoggingMixIn
except ImportError:
    print("Installa fusepy: sudo apt install python3-fusepy"); sys.exit(1)

from stat import S_IFDIR, S_IFREG

# ─── Costanti API ─────────────────────────────────────────────────────────────

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

def auth_headers(token: str) -> dict:
    return {**HEADERS, "Authorization": f"Bearer {token}"}

# ─── Autenticazione ───────────────────────────────────────────────────────────

def get_providers(session: requests.Session, username: str) -> list:
    r = session.get(
        f"{API_V10}/auth/login",
        params={"username": username, "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET, "language": "en"},
        headers=HEADERS, timeout=30,
    )
    r.raise_for_status()
    data = r.json()
    logging.debug(f"Providers raw: {json.dumps(data)[:400]}")
    providers = data.get("providers", [])
    if not providers:
        raise RuntimeError(f"Nessun provider per '{username}'. Verifica che l'account esista.")
    return providers


def login_with_provider(session: requests.Session, username: str,
                        password: str, provider: dict) -> tuple | None:
    alias   = provider["alias"]
    shop_id = provider.get("shop_id", "1")
    r = session.post(
        f"{API_V10}/auth/login/{alias}",
        data={"username": username, "password": password,
              "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET,
              "grant_type": "password", "shop_id": shop_id},
        headers={**HEADERS, "Content-Type": "application/x-www-form-urlencoded"},
        timeout=30,
    )
    logging.debug(f"Login {alias} → {r.status_code}: {r.text[:300]}")
    if r.status_code in (401, 403):
        return None
    r.raise_for_status()
    data  = r.json()
    token = data.get("access_token") or data.get("token") or data.get("auth_token")
    if not token:
        return None
    return token, data.get("refresh_token"), data.get("expires_in", 3600)


def login(session: requests.Session, username: str, password: str) -> tuple:
    logging.info(f"Recupero provider per {username}...")
    providers = get_providers(session, username)
    logging.info(f"Provider: {[p['alias'] for p in providers]}")
    for provider in providers:
        logging.info(f"Provo: {provider['alias']} ...")
        result = login_with_provider(session, username, password, provider)
        if result:
            logging.info(f"Login OK con: {provider['alias']}")
            return result
    raise RuntimeError("Login fallito con tutti i provider. Verifica le credenziali.")


def renew_token(session: requests.Session, token: str, refresh_tok: str) -> str:
    try:
        r = session.post(
            f"{API_V10}/auth/renew-token",
            data={"grant_type": "refresh_token", "refresh_token": refresh_tok,
                  "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET},
            headers={**HEADERS, "Content-Type": "application/x-www-form-urlencoded"},
            timeout=30,
        )
        if r.ok:
            d = r.json()
            return d.get("access_token") or d.get("token") or token
    except Exception as e:
        logging.warning(f"Rinnovo token fallito: {e}")
    return token

# ─── Lista libri ──────────────────────────────────────────────────────────────

def get_all_books(session: requests.Session, token: str) -> list:
    all_books, page = [], 1
    logging.info("Recupero lista libri...")
    while True:
        r = session.get(
            f"{API_V10}/books",
            headers=auth_headers(token),
            params={"page": page, "per_page": 100},
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()
        logging.debug(f"Pagina {page}: {str(data)[:300]}")
        if isinstance(data, list):
            books    = data
            has_more = len(data) == 100
        else:
            books    = data.get("items") or data.get("books") or data.get("data") or []
            total    = int(data.get("total") or data.get("count") or len(books))
            has_more = (len(all_books) + len(books)) < total
        all_books.extend(books)
        logging.info(f"  Pagina {page}: {len(books)} libri (tot: {len(all_books)})")
        if not books or not has_more:
            break
        page += 1
    logging.info(f"Totale: {len(all_books)} libri")
    return all_books

# ─── Nomi file ────────────────────────────────────────────────────────────────

def safe_filename(name: str) -> str:
    for ch in r'\/:*?"<>|':
        name = name.replace(ch, "_")
    return name.strip()[:180]


def book_to_filename(book: dict) -> str:
    book_id = str(book.get("id") or "")
    title   = book.get("title") or book.get("name") or f"libro_{book_id}"
    author  = book.get("author") or book.get("authors") or ""
    path    = book.get("path") or ""
    mime    = book.get("mime_type") or ""

    if isinstance(author, list):
        author = ", ".join(
            a.get("name", str(a)) if isinstance(a, dict) else str(a)
            for a in author
        )

    ext = (Path(path).suffix.lstrip(".").lower() if path else
           (mime.split("/")[-1].replace("x-", "").replace("+zip", "").lower()
            if mime else "epub"))

    parts    = ([safe_filename(author)] if author else []) + [safe_filename(title)]
    filename = " - ".join(parts) + f".{ext}"
    return filename

# ─── Filesystem FUSE ──────────────────────────────────────────────────────────

class PocketBookCloudFS(LoggingMixIn, Operations):
    """Filesystem FUSE in sola lettura che espone la libreria PocketBook Cloud."""

    def __init__(self, email: str, password: str):
        self.email    = email
        self.password = password
        self._now     = time.time()
        self.session  = requests.Session()
        self.session.headers.update(HEADERS)

        # Autenticazione
        self.token, self.refresh_tok, expires_in = login(
            self.session, email, password
        )
        self._token_expiry = time.time() + expires_in - 60

        # Costruisce mappa  nome_file → metadati libro
        raw_books   = get_all_books(self.session, self.token)
        self.files: dict = {}
        seen: dict       = {}

        for book in raw_books:
            name = book_to_filename(book)
            base = name
            if base in seen:
                seen[base] += 1
                stem, ext = os.path.splitext(base)
                name = f"{stem}_{seen[base]}{ext}"
            else:
                seen[base] = 0

            self.files[name] = {
                "path":    book.get("path", ""),
                "id":      str(book.get("id", "")),
                "size":    book.get("bytes", 0) or book.get("size", 0),
                "mime":    book.get("mime_type", ""),
            }

        # Cache in memoria (max ~50 MB, poi svuota)
        self._cache: dict = {}

    # ── Token refresh automatico ────────────────────────────────────────────

    def _ensure_token(self):
        if self.refresh_tok and time.time() > self._token_expiry:
            logging.info("Token scaduto, rinnovo...")
            self.token = renew_token(self.session, self.token, self.refresh_tok)
            self._token_expiry = time.time() + 3540

    # ── Attributi ──────────────────────────────────────────────────────────

    def getattr(self, path: str, fh=None) -> dict:
        if path == "/":
            return {"st_mode": S_IFDIR | 0o555, "st_nlink": 2,
                    "st_atime": self._now, "st_ctime": self._now, "st_mtime": self._now}
        name = path.lstrip("/")
        if name not in self.files:
            raise FuseOSError(errno.ENOENT)
        return {"st_mode": S_IFREG | 0o444, "st_nlink": 1,
                "st_size":  self.files[name]["size"],
                "st_atime": self._now, "st_ctime": self._now, "st_mtime": self._now}

    # ── Directory ──────────────────────────────────────────────────────────

    def readdir(self, path: str, fh):
        if path != "/":
            raise FuseOSError(errno.ENOENT)
        return [".", ".."] + list(self.files.keys())

    # ── Download on-demand ────────────────────────────────────────────────

    def _fetch(self, name: str) -> bytes:
        if name in self._cache:
            return self._cache[name]

        self._ensure_token()
        meta    = self.files[name]
        fpath   = meta["path"]
        book_id = meta["id"]
        ext     = Path(name).suffix.lstrip(".").lower() or "epub"

        # Candidati URL di download (stesso ordine dello script funzionante)
        candidates = []
        if fpath:
            enc = quote(fpath, safe="/")
            candidates += [
                f"{API_V10}/files{enc}",
                f"{API_V10}/file{enc}",
                f"{API_V10}/storage{enc}",
                f"{API_V10}/user/files{enc}",
            ]
        if book_id:
            candidates += [
                f"{API_V10}/books/{book_id}/download",
                f"{API_V10}/books/{book_id}/download?format={ext}",
                f"{API_V10}/books/{book_id}/file",
            ]

        logging.debug(f"Download '{name}': provo {len(candidates)} URL")

        for url in candidates:
            try:
                resp = self.session.get(
                    url, headers=auth_headers(self.token),
                    stream=True, timeout=120, allow_redirects=True,
                )
                logging.debug(f"  {url} → {resp.status_code}")
                if resp.status_code == 404:
                    continue
                resp.raise_for_status()

                ct = resp.headers.get("Content-Type", "")
                if "json" in ct:
                    body = resp.content
                    if b'"error' in body:
                        continue
                    try:
                        d    = json.loads(body)
                        real = d.get("url") or d.get("download_url")
                        if real:
                            resp = self.session.get(
                                real, headers=auth_headers(self.token),
                                stream=True, timeout=120,
                            )
                            resp.raise_for_status()
                        else:
                            continue
                    except Exception:
                        continue

                data = resp.content

                # Verifica che non sia una risposta di errore JSON
                if len(data) < 200 and b'"error' in data:
                    continue

                # Aggiorna dimensione reale
                self.files[name]["size"] = len(data)

                # Cache: svuota se supera 50 MB
                if sum(len(v) for v in self._cache.values()) + len(data) > 50 * 1024 * 1024:
                    self._cache.clear()
                self._cache[name] = data
                logging.info(f"Download OK: {name} ({len(data)//1024} KB)")
                return data

            except Exception as e:
                logging.debug(f"  Errore su {url}: {e}")
                continue

        logging.error(f"Impossibile scaricare '{name}'")
        raise FuseOSError(errno.EIO)

    def read(self, path: str, length: int, offset: int, fh) -> bytes:
        name = path.lstrip("/")
        if name not in self.files:
            raise FuseOSError(errno.ENOENT)
        data = self._fetch(name)
        return data[offset:offset + length]

    # ── Sola lettura ───────────────────────────────────────────────────────

    def write(self, path, buf, offset, fh):  raise FuseOSError(errno.EROFS)
    def create(self, path, mode, fi=None):   raise FuseOSError(errno.EROFS)
    def unlink(self, path):                   raise FuseOSError(errno.EROFS)
    def mkdir(self, path, mode):              raise FuseOSError(errno.EROFS)

# ─── Entry point ──────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Monta PocketBook Cloud come filesystem FUSE (sola lettura).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Smonta con:  fusermount -u <mountpoint>",
    )
    parser.add_argument("--email",      "-u", required=True,  help="Email account PocketBook")
    parser.add_argument("--password",   "-p", required=True,  help="Password account PocketBook")
    parser.add_argument("--mountpoint", "-m", required=True,  help="Directory di mount (deve esistere)")
    parser.add_argument("--foreground",       action="store_true", help="Esegui in foreground")
    parser.add_argument("--debug",            action="store_true", help="Logging dettagliato")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    if not os.path.isdir(args.mountpoint):
        print(f"Errore: il mountpoint '{args.mountpoint}' non esiste.")
        print(f"Crealo con:  mkdir -p {args.mountpoint}")
        sys.exit(1)

    logging.info(f"Avvio PocketBook Cloud FUSE su '{args.mountpoint}'")
    fs = PocketBookCloudFS(args.email, args.password)
    logging.info(f"Pronti: {len(fs.files)} libri montati. Ctrl+C per smontare.")

    FUSE(
        fs,
        args.mountpoint,
        nothreads=True,
        foreground=args.foreground,
        ro=True,
        allow_other=False,
    )


if __name__ == "__main__":
    main()
