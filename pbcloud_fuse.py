#!/usr/bin/env python3
"""
PocketBook Cloud FUSE mount — v4
Monta la libreria PocketBook Cloud come filesystem locale (sola lettura).

Novità v4:
  - Cache su disco (~/.cache/pbcloud/): i libri già scaricati sopravvivono al ri-mount
  - Logging del campo raw "author" per diagnosticare book_to_filename()
  - Refresh token automatico in background via threading.Timer

Dipendenze:
  sudo apt install python3-fusepy
  pip install requests   (oppure sudo apt install python3-requests)

Utilizzo:
  python3 pbcloud_fuse.py --email TUA@EMAIL --password TUAPASSWORD \
      --mountpoint ./pocketbook_libri [--foreground] [--debug] \
      [--cache-dir ~/.cache/pbcloud] [--cache-ttl 86400]

Smonta:
  fusermount -u ./pocketbook_libri
"""

import os
import sys
import errno
import time
import json
import hashlib
import argparse
import logging
import threading
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
    prev_ids: set = set()

    while True:
        r = session.get(
            f"{API_V10}/books",
            headers=auth_headers(token),
            params={"page": page, "per_page": 100, "limit": 100, "offset": (page - 1) * 100},
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()

        if isinstance(data, list):
            books    = data
            has_more = len(data) == 100
        else:
            books    = data.get("items") or data.get("books") or data.get("data") or []
            total    = int(data.get("total") or data.get("count") or len(books))
            has_more = (len(all_books) + len(books)) < total

        if not books:
            break

        current_ids = {str(b.get("id", "")) for b in books}
        if current_ids and current_ids.issubset(prev_ids):
            logging.warning(f"Pagina {page} restituisce ID già visti — stop.")
            break
        prev_ids.update(current_ids)

        # ── LOG DIAGNOSTICO CAMPO AUTHOR (v4) ─────────────────────────────
        # Stampa il valore raw di tutti i campi autore candidati per i primi
        # 5 libri di ogni pagina.  Eseguire con --debug per vederli.
        # Poi adattare book_to_filename() al campo che contiene dati reali.
        for b in books[:5]:
            logging.debug(
                f"[author-raw] id={b.get('id')} "
                f"author={b.get('author')!r} "
                f"authors={b.get('authors')!r} "
                f"author_name={b.get('author_name')!r} "
                f"creator={b.get('creator')!r}"
            )
        # ──────────────────────────────────────────────────────────────────

        all_books.extend(books)
        logging.info(f"  Pagina {page}: {len(books)} libri (tot: {len(all_books)})")

        if not has_more:
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

    # Prova tutti i campi noti in ordine di priorità.
    # Se l'API usa un campo diverso, il log [author-raw] ti mostrerà quale.
    author = (book.get("author") or book.get("authors") or
              book.get("author_name") or book.get("creator") or "")

    path = book.get("path") or ""
    mime = book.get("mime_type") or ""

    if isinstance(author, list):
        author = ", ".join(
            a.get("name", str(a)) if isinstance(a, dict) else str(a)
            for a in author
        )
    elif isinstance(author, dict):
        author = author.get("name") or author.get("full_name") or str(author)

    ext = (Path(path).suffix.lstrip(".").lower() if path else
           (mime.split("/")[-1].replace("x-", "").replace("+zip", "").lower()
            if mime else "epub"))

    parts    = ([safe_filename(author)] if author else []) + [safe_filename(title)]
    filename = " - ".join(parts) + f".{ext}"
    return filename

# ─── Cache su disco (v4) ──────────────────────────────────────────────────────

class DiskCache:
    """
    Cache persistente su disco per contenuti dei libri scaricati.

    Struttura directory:
      <cache_dir>/
        <sha256(filename)>.bin    — dati binari del libro
        <sha256(filename)>.meta   — JSON: nome, size, timestamp
        booklist.json             — lista libri + timestamp ultimo fetch
    """

    def __init__(self, cache_dir: Path, ttl_seconds: int = 86400):
        self.cache_dir   = Path(cache_dir)
        self.ttl_seconds = ttl_seconds
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        logging.info(f"Cache su disco: {self.cache_dir} (TTL={ttl_seconds}s)")

    def _key(self, name: str) -> str:
        return hashlib.sha256(name.encode()).hexdigest()

    # ── Booklist ──────────────────────────────────────────────────────────────

    def load_booklist(self) -> list | None:
        """Restituisce la lista libri se non scaduta, altrimenti None."""
        bl_path = self.cache_dir / "booklist.json"
        if not bl_path.exists():
            return None
        try:
            with bl_path.open() as f:
                data = json.load(f)
            age = time.time() - data.get("timestamp", 0)
            if age > self.ttl_seconds:
                logging.info(f"Booklist scaduta ({age:.0f}s > TTL {self.ttl_seconds}s).")
                return None
            books = data.get("books", [])
            logging.info(f"Booklist caricata da disco ({len(books)} libri, {age:.0f}s fa).")
            return books
        except Exception as e:
            logging.warning(f"Errore lettura booklist: {e}")
            return None

    def save_booklist(self, books: list) -> None:
        bl_path = self.cache_dir / "booklist.json"
        try:
            with bl_path.open("w") as f:
                json.dump({"timestamp": time.time(), "books": books}, f)
            logging.info(f"Booklist salvata su disco ({len(books)} libri).")
        except Exception as e:
            logging.warning(f"Errore salvataggio booklist: {e}")

    # ── Contenuto libri ───────────────────────────────────────────────────────

    def get(self, name: str) -> bytes | None:
        key       = self._key(name)
        bin_path  = self.cache_dir / f"{key}.bin"
        meta_path = self.cache_dir / f"{key}.meta"
        if not bin_path.exists() or not meta_path.exists():
            return None
        try:
            with meta_path.open() as f:
                meta = json.load(f)
            age = time.time() - meta.get("timestamp", 0)
            if age > self.ttl_seconds:
                logging.debug(f"Cache disco scaduta per '{name}' ({age:.0f}s).")
                bin_path.unlink(missing_ok=True)
                meta_path.unlink(missing_ok=True)
                return None
            data = bin_path.read_bytes()
            logging.info(f"Cache HIT (disco): {name} ({len(data)//1024} KB)")
            return data
        except Exception as e:
            logging.warning(f"Errore lettura cache disco '{name}': {e}")
            return None

    def put(self, name: str, data: bytes) -> None:
        key       = self._key(name)
        bin_path  = self.cache_dir / f"{key}.bin"
        meta_path = self.cache_dir / f"{key}.meta"
        try:
            bin_path.write_bytes(data)
            with meta_path.open("w") as f:
                json.dump({"name": name, "size": len(data),
                           "timestamp": time.time()}, f)
            logging.debug(f"Cache disco WRITE: {name} ({len(data)//1024} KB)")
        except Exception as e:
            logging.warning(f"Errore scrittura cache disco '{name}': {e}")

    def invalidate(self, name: str) -> None:
        key = self._key(name)
        for suffix in (".bin", ".meta"):
            p = self.cache_dir / f"{key}{suffix}"
            p.unlink(missing_ok=True)

# ─── Filesystem FUSE ──────────────────────────────────────────────────────────

class PocketBookCloudFS(LoggingMixIn, Operations):
    """Filesystem FUSE in sola lettura che espone la libreria PocketBook Cloud."""

    def __init__(self, email: str, password: str,
                 cache_dir: Path, cache_ttl: int):
        self.email    = email
        self.password = password
        self._now     = time.time()
        self.session  = requests.Session()
        self.session.headers.update(HEADERS)

        # Cache su disco (v4)
        self.disk_cache = DiskCache(cache_dir, cache_ttl)

        # Autenticazione
        self.token, self.refresh_tok, expires_in = login(
            self.session, email, password
        )
        self._token_expiry = time.time() + expires_in - 60
        self._token_lock   = threading.Lock()

        # Carica lista libri (da cache su disco o dall'API)
        raw_books = self.disk_cache.load_booklist()
        if raw_books is None:
            raw_books = get_all_books(self.session, self.token)
            self.disk_cache.save_booklist(raw_books)

        self.files: dict = {}
        seen: dict       = {}
        for book in raw_books:
            base = book_to_filename(book)
            if base not in seen:
                seen[base] = 0
                name = base
            else:
                seen[base] += 1
                stem, ext = os.path.splitext(base)
                name = f"{stem}_{seen[base]}{ext}"
            self.files[name] = {
                "path": book.get("path", ""),
                "id":   str(book.get("id", "")),
                "size": book.get("bytes", 0) or book.get("size", 0),
                "mime": book.get("mime_type", ""),
            }

        # Hot-layer in memoria (max ~50 MB)
        self._mem_cache: dict = {}

        # Avvia refresh token in background (v4)
        self._start_token_refresh_timer(expires_in)

    # ── Refresh token in background (v4) ────────────────────────────────────

    def _start_token_refresh_timer(self, expires_in: int) -> None:
        """
        Pianifica il rinnovo del token ~60 s prima della scadenza.
        Timer daemon=True: non impedisce l'uscita del processo.
        """
        delay = max(expires_in - 60, 30)
        logging.info(f"Token refresh pianificato tra {delay}s.")
        self._refresh_timer = threading.Timer(delay, self._background_refresh)
        self._refresh_timer.daemon = True
        self._refresh_timer.start()

    def _background_refresh(self) -> None:
        """Callback del timer: rinnova il token e ripianifica per il ciclo successivo."""
        if not self.refresh_tok:
            logging.warning("Nessun refresh_token, skip background refresh.")
            return
        logging.info("Background token refresh in corso...")
        with self._token_lock:
            new_token = renew_token(self.session, self.token, self.refresh_tok)
            if new_token != self.token:
                self.token         = new_token
                self._token_expiry = time.time() + 3540
                logging.info("Token rinnovato in background con successo.")
            else:
                logging.warning("Background refresh: token invariato (possibile errore).")
        # Ripianifica automaticamente
        self._start_token_refresh_timer(3600)

    def _cancel_refresh_timer(self) -> None:
        if hasattr(self, "_refresh_timer"):
            self._refresh_timer.cancel()

    # ── Token on-demand (fallback di sicurezza) ──────────────────────────────

    def _ensure_token(self) -> None:
        with self._token_lock:
            if self.refresh_tok and time.time() > self._token_expiry:
                logging.info("Token scaduto (on-demand), rinnovo sincrono...")
                self.token         = renew_token(self.session, self.token, self.refresh_tok)
                self._token_expiry = time.time() + 3540

    # ── Attributi ────────────────────────────────────────────────────────────

    def getattr(self, path: str, fh=None) -> dict:
        if path == "/":
            return {"st_mode": S_IFDIR | 0o555, "st_nlink": 2,
                    "st_atime": self._now, "st_ctime": self._now, "st_mtime": self._now}
        name = path.lstrip("/")
        if name not in self.files:
            raise FuseOSError(errno.ENOENT)
        return {"st_mode": S_IFREG | 0o444, "st_nlink": 1,
                "st_size": self.files[name]["size"],
                "st_atime": self._now, "st_ctime": self._now, "st_mtime": self._now}

    # ── Directory ────────────────────────────────────────────────────────────

    def readdir(self, path: str, fh):
        if path != "/":
            raise FuseOSError(errno.ENOENT)
        return [".", ".."] + list(self.files.keys())

    # ── Download on-demand ───────────────────────────────────────────────────

    def _fetch(self, name: str) -> bytes:
        # 1. Hot layer: RAM
        if name in self._mem_cache:
            logging.debug(f"Cache HIT (memoria): {name}")
            return self._mem_cache[name]

        # 2. Cache su disco
        data = self.disk_cache.get(name)
        if data is not None:
            self.files[name]["size"] = len(data)
            total_mem = sum(len(v) for v in self._mem_cache.values())
            if total_mem + len(data) <= 50 * 1024 * 1024:
                self._mem_cache[name] = data
            return data

        # 3. Rete
        self._ensure_token()
        meta    = self.files[name]
        fpath   = meta["path"]
        book_id = meta["id"]
        ext     = Path(name).suffix.lstrip(".").lower() or "epub"

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
                if len(data) < 200 and b'"error' in data:
                    continue

                self.files[name]["size"] = len(data)
                self.disk_cache.put(name, data)

                total_mem = sum(len(v) for v in self._mem_cache.values())
                if total_mem + len(data) > 50 * 1024 * 1024:
                    self._mem_cache.clear()
                self._mem_cache[name] = data

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

    # ── Sola lettura ─────────────────────────────────────────────────────────

    def write(self,  path, buf, offset, fh): raise FuseOSError(errno.EROFS)
    def create(self, path, mode, fi=None):   raise FuseOSError(errno.EROFS)
    def unlink(self, path):                  raise FuseOSError(errno.EROFS)
    def mkdir(self,  path, mode):            raise FuseOSError(errno.EROFS)

    # ── Cleanup ──────────────────────────────────────────────────────────────

    def destroy(self, path):
        """Chiamato da FUSE allo smontaggio: cancella il timer background."""
        self._cancel_refresh_timer()
        logging.info("Filesystem smontato, timer refresh cancellato.")

# ─── Entry point ──────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Monta PocketBook Cloud come filesystem FUSE (sola lettura).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Smonta con: fusermount -u <mountpoint>",
    )
    parser.add_argument("--email",      "-u", required=True,
                        help="Email account PocketBook")
    parser.add_argument("--password",   "-p", required=True,
                        help="Password account PocketBook")
    parser.add_argument("--mountpoint", "-m", required=True,
                        help="Directory di mount (deve esistere)")
    parser.add_argument("--foreground", action="store_true",
                        help="Esegui in foreground")
    parser.add_argument("--debug",      action="store_true",
                        help="Logging dettagliato (mostra [author-raw])")
    parser.add_argument("--cache-dir",
                        default=str(Path.home() / ".cache" / "pbcloud"),
                        help="Directory cache su disco (default: ~/.cache/pbcloud)")
    parser.add_argument("--cache-ttl",  type=int, default=86400,
                        help="TTL cache in secondi (default: 86400 = 24h)")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    if not os.path.isdir(args.mountpoint):
        print(f"Errore: il mountpoint '{args.mountpoint}' non esiste.")
        print(f"Crealo con: mkdir -p {args.mountpoint}")
        sys.exit(1)

    logging.info(f"Avvio PocketBook Cloud FUSE su '{args.mountpoint}'")
    fs = PocketBookCloudFS(
        email     = args.email,
        password  = args.password,
        cache_dir = Path(args.cache_dir),
        cache_ttl = args.cache_ttl,
    )
    logging.info(f"Pronti: {len(fs.files)} libri montati. Ctrl+C per smontare.")

    FUSE(
        fs,
        args.mountpoint,
        nothreads   = False,   # threading abilitato per il timer background
        foreground  = args.foreground,
        ro          = True,
        allow_other = False,
    )

if __name__ == "__main__":
    main()
