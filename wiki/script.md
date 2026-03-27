``` python
MCP File Server Aziendale

Variabili d'ambiente richieste:
  MCP_BASE_DIR        percorso della cartella documenti
  MCP_CLIENT_ID       identificativo OAuth (es. mcp-aziendale)
  MCP_CLIENT_SECRET   segreto OAuth (genera con: python3 -c "import secrets; print(secrets.token_hex(32))")
  MCP_ALLOWED_HOST    dominio pubblico (es. mcp.tuaazienda.com)
  MCP_SSL_CERTFILE    percorso al certificato SSL (fullchain.pem)
  MCP_SSL_KEYFILE     percorso alla chiave privata SSL (privkey.pem)

Variabili opzionali:
  MCP_TOKEN_EXPIRY    durata token in secondi (default: 86400)
  MCP_DB_PATH         percorso file SQLite (default: /opt/mcp-fileserver/mcp.db)

Il database SQLite contiene:
  - tokens           : token OAuth attivi (sopravvivono al riavvio del processo)
  - file_index       : metadati dei file indicizzati (mtime, size)
  - file_content     : indice full-text FTS5 per search_content

All'avvio il server si mette in ascolto immediatamente; l'indicizzazione
avviene in un thread separato senza bloccare le richieste. Un watcher in
background (watchdog) mantiene l'indice aggiornato in tempo reale quando
i file vengono aggiunti, modificati o cancellati.
"""

import os
import secrets
import sqlite3
import time
import hashlib
import base64
import asyncio
import logging
import threading
from collections import defaultdict
from contextlib import asynccontextmanager
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.routing import Route, Mount
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, Response, RedirectResponse

# ---------------------------------------------------------------------------
# LOGGING
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("mcp-fileserver")

# ---------------------------------------------------------------------------
# CONFIGURAZIONE DA VARIABILI D'AMBIENTE
# ---------------------------------------------------------------------------

def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(
            f"Variabile d'ambiente obbligatoria non impostata: {name}\n"
            "Vedi il file /etc/mcp-fileserver/secrets"
        )
    return value


BASE_DIR            = _require_env("MCP_BASE_DIR")
OAUTH_CLIENT_ID     = _require_env("MCP_CLIENT_ID")
OAUTH_CLIENT_SECRET = _require_env("MCP_CLIENT_SECRET")
ALLOWED_HOST        = _require_env("MCP_ALLOWED_HOST")
SSL_CERTFILE        = _require_env("MCP_SSL_CERTFILE")
SSL_KEYFILE         = _require_env("MCP_SSL_KEYFILE")
TOKEN_EXPIRY        = int(os.environ.get("MCP_TOKEN_EXPIRY", "86400"))
DB_PATH             = os.environ.get("MCP_DB_PATH", "/opt/mcp-fileserver/mcp.db")

# File piu' grandi di questa soglia vengono saltati dall'indicizzazione.
MAX_INDEX_FILE_BYTES = 50 * 1024 * 1024  # 50 MB

log.info(
    "BASE_DIR=%s ALLOWED_HOST=%s TOKEN_EXPIRY=%s DB_PATH=%s",
    BASE_DIR, ALLOWED_HOST, TOKEN_EXPIRY, DB_PATH,
)

# ---------------------------------------------------------------------------
# DATABASE — INIT E LOCK
# ---------------------------------------------------------------------------

# Lock globale per tutte le scritture sul DB.
# SQLite in WAL mode supporta letture concorrenti, ma le scritture
# devono essere serializzate per evitare errori "database is locked".
_db_write_lock = threading.Lock()


def _db_connect() -> sqlite3.Connection:
    """Apre una connessione SQLite con WAL abilitato."""
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def _db_init() -> None:
    """
    Crea tutte le tabelle necessarie se non esistono.

    - tokens       : token OAuth persistenti
    - file_index   : mtime e size di ogni file indicizzato
    - file_content : indice FTS5 full-text (path + testo estratto)
    """
    with _db_write_lock:
        conn = _db_connect()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS tokens (
                token   TEXT PRIMARY KEY,
                expires REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS file_index (
                path        TEXT PRIMARY KEY,
                mtime       REAL NOT NULL,
                size        INTEGER NOT NULL,
                indexed_at  REAL NOT NULL
            );

            CREATE VIRTUAL TABLE IF NOT EXISTS file_content
            USING fts5(
                path     UNINDEXED,
                content,
                tokenize = 'unicode61 remove_diacritics 1'
            );
        """)
        conn.commit()
        conn.close()

# ---------------------------------------------------------------------------
# DATABASE — TOKEN
# ---------------------------------------------------------------------------

def _db_token_save(token: str, expires: float) -> None:
    with _db_write_lock:
        conn = _db_connect()
        conn.execute(
            "INSERT OR REPLACE INTO tokens (token, expires) VALUES (?, ?)",
            (token, expires),
        )
        conn.commit()
        conn.close()


def _db_token_delete(token: str) -> None:
    with _db_write_lock:
        conn = _db_connect()
        conn.execute("DELETE FROM tokens WHERE token = ?", (token,))
        conn.commit()
        conn.close()


def _db_token_cleanup(now: float) -> int:
    with _db_write_lock:
        conn = _db_connect()
        count = conn.execute(
            "DELETE FROM tokens WHERE expires <= ?", (now,)
        ).rowcount
        conn.commit()
        conn.close()
    return count


def _db_load_active_tokens() -> dict[str, float]:
    now  = time.time()
    conn = _db_connect()
    rows = conn.execute(
        "SELECT token, expires FROM tokens WHERE expires > ?", (now,)
    ).fetchall()
    conn.close()
    return {token: expires for token, expires in rows}

# ---------------------------------------------------------------------------
# DATABASE — INDICE FTS
# ---------------------------------------------------------------------------

def _db_index_status(rel_path: str) -> tuple[float, int] | None:
    """
    Restituisce (mtime, size) dell'ultima indicizzazione del file,
    oppure None se il file non e' ancora nell'indice.
    """
    conn = _db_connect()
    row  = conn.execute(
        "SELECT mtime, size FROM file_index WHERE path = ?", (rel_path,)
    ).fetchone()
    conn.close()
    return row


def _db_upsert_file(rel_path: str, testo: str, mtime: float, size: int) -> None:
    """Inserisce o aggiorna un file nell'indice FTS."""
    now = time.time()
    with _db_write_lock:
        conn = _db_connect()
        conn.execute("DELETE FROM file_content WHERE path = ?", (rel_path,))
        conn.execute(
            "INSERT INTO file_content (path, content) VALUES (?, ?)",
            (rel_path, testo),
        )
        conn.execute(
            "INSERT OR REPLACE INTO file_index (path, mtime, size, indexed_at) "
            "VALUES (?, ?, ?, ?)",
            (rel_path, mtime, size, now),
        )
        conn.commit()
        conn.close()


def _db_remove_file(rel_path: str) -> None:
    """Rimuove un file dall'indice (chiamato quando il file viene cancellato)."""
    with _db_write_lock:
        conn = _db_connect()
        conn.execute("DELETE FROM file_content WHERE path = ?", (rel_path,))
        conn.execute("DELETE FROM file_index WHERE path = ?", (rel_path,))
        conn.commit()
        conn.close()


def _db_search(keyword: str, max_results: int) -> list[tuple[str, str]]:
    """
    Interroga l'indice FTS5 e restituisce una lista di (path, estratto).
    L'estratto e' generato dalla funzione snippet() di SQLite FTS5:
    la parola trovata e' racchiusa tra parentesi quadre [].
    """
    conn = _db_connect()
    rows = conn.execute(
        """
        SELECT path,
               snippet(file_content, 1, '[', ']', '...', 25)
        FROM   file_content
        WHERE  file_content MATCH ?
        LIMIT  ?
        """,
        (keyword, max_results),
    ).fetchall()
    conn.close()
    return rows


def _db_index_count() -> int:
    conn  = _db_connect()
    count = conn.execute("SELECT COUNT(*) FROM file_index").fetchone()[0]
    conn.close()
    return count

# ---------------------------------------------------------------------------
# STATO IN MEMORIA
# ---------------------------------------------------------------------------

auth_codes: dict[str, dict] = {}
active_tokens: dict[str, float] = {}

# Stato dell'indicizzazione iniziale, aggiornato dal thread di indicizzazione
_index_state = {
    "running": False,
    "total":   0,
    "done":    0,
    "errors":  0,
}

# ---------------------------------------------------------------------------
# INDICIZZATORE
# ---------------------------------------------------------------------------

ESTENSIONI_LEGGIBILI = {
    ".pdf", ".docx", ".xlsx", ".xls",
    ".txt", ".md", ".csv", ".json", ".xml", ".html", ".htm",
}


def _needs_reindex(rel_path: str, full_path: str) -> bool:
    """
    Restituisce True se il file non e' nell'indice o e' cambiato
    rispetto all'ultima indicizzazione (mtime o size diversi).
    """
    try:
        stat = os.stat(full_path)
    except OSError:
        return False
    status = _db_index_status(rel_path)
    if status is None:
        return True
    indexed_mtime, indexed_size = status
    return stat.st_mtime != indexed_mtime or stat.st_size != indexed_size


def _index_single_file(full_path: str, rel_path: str) -> bool:
    """
    Estrae il testo da un file e lo inserisce nell'indice.
    Restituisce True in caso di successo.
    """
    try:
        stat = os.stat(full_path)
        if stat.st_size > MAX_INDEX_FILE_BYTES:
            log.info("Indicizzazione saltata (file troppo grande): %s", rel_path)
            return False
        testo = _estrai_testo(full_path, max_chars=500_000)
        if testo.startswith("[Errore") or testo.startswith("[PDF protetto"):
            log.warning("Indicizzazione saltata per errore estrazione: %s", rel_path)
            return False
        _db_upsert_file(rel_path, testo, stat.st_mtime, stat.st_size)
        return True
    except Exception as e:
        log.error("Errore indicizzazione %s: %s", rel_path, e)
        return False


def _run_initial_indexing() -> None:
    """
    Scansiona BASE_DIR e indicizza tutti i file non ancora presenti
    o modificati rispetto all'ultima indicizzazione.
    Viene eseguito in un thread separato all'avvio, senza bloccare il server.
    Alla seconda esecuzione (riavvio del processo) indicizza solo i file
    cambiati, quindi e' molto piu' veloce.
    """
    _index_state["running"] = True
    log.info("Indicizzazione iniziale avviata...")

    da_indicizzare = []
    for root, dirs, files in os.walk(BASE_DIR):
        dirs.sort()
        for nome in sorted(files):
            ext = Path(nome).suffix.lower()
            if ext not in ESTENSIONI_LEGGIBILI:
                continue
            full_path = os.path.join(root, nome)
            rel_path  = os.path.relpath(full_path, BASE_DIR)
            if _needs_reindex(rel_path, full_path):
                da_indicizzare.append((full_path, rel_path))

    _index_state["total"]  = len(da_indicizzare)
    _index_state["done"]   = 0
    _index_state["errors"] = 0

    log.info("File da indicizzare: %d", len(da_indicizzare))

    for full_path, rel_path in da_indicizzare:
        ok = _index_single_file(full_path, rel_path)
        if ok:
            _index_state["done"] += 1
        else:
            _index_state["errors"] += 1

    _index_state["running"] = False
    log.info(
        "Indicizzazione completata: %d indicizzati, %d errori, %d totale nell'indice",
        _index_state["done"],
        _index_state["errors"],
        _db_index_count(),
    )


def _start_indexing_thread() -> threading.Thread:
    t = threading.Thread(target=_run_initial_indexing, daemon=True, name="indexer")
    t.start()
    return t

# ---------------------------------------------------------------------------
# WATCHER — aggiornamento indice in tempo reale
# ---------------------------------------------------------------------------

def _start_file_watcher() -> None:
    """
    Avvia un observer watchdog che aggiorna l'indice in tempo reale
    quando i file vengono creati, modificati, cancellati o spostati.
    Se watchdog non e' installato, registra un avviso e continua senza watcher.
    """
    try:
        from watchdog.observers import Observer
        from watchdog.events import FileSystemEventHandler
    except ImportError:
        log.warning(
            "watchdog non installato: l'indice non si aggiornerà in tempo reale. "
            "Installa con: pip install watchdog"
        )
        return

    class _Handler(FileSystemEventHandler):
        def _rel(self, path: str) -> str:
            return os.path.relpath(path, BASE_DIR)

        def _eligible(self, path: str) -> bool:
            return Path(path).suffix.lower() in ESTENSIONI_LEGGIBILI

        def on_created(self, event):
            if event.is_directory or not self._eligible(event.src_path):
                return
            rel = self._rel(event.src_path)
            log.info("Watcher: nuovo file, indicizzazione: %s", rel)
            _index_single_file(event.src_path, rel)

        def on_modified(self, event):
            if event.is_directory or not self._eligible(event.src_path):
                return
            rel = self._rel(event.src_path)
            log.info("Watcher: file modificato, re-indicizzazione: %s", rel)
            _index_single_file(event.src_path, rel)

        def on_deleted(self, event):
            if event.is_directory or not self._eligible(event.src_path):
                return
            rel = self._rel(event.src_path)
            log.info("Watcher: file eliminato, rimozione dall'indice: %s", rel)
            _db_remove_file(rel)

        def on_moved(self, event):
            if event.is_directory:
                return
            if self._eligible(event.src_path):
                _db_remove_file(self._rel(event.src_path))
            if self._eligible(event.dest_path):
                _index_single_file(event.dest_path, self._rel(event.dest_path))

    observer = Observer()
    observer.schedule(_Handler(), BASE_DIR, recursive=True)
    observer.daemon = True
    observer.start()
    log.info("Watcher avviato su %s", BASE_DIR)

# ---------------------------------------------------------------------------
# RATE LIMITING SUGLI ENDPOINT PUBBLICI
# ---------------------------------------------------------------------------

_rate_lock     = threading.Lock()
_rate_counters: dict[str, list[float]] = defaultdict(list)

RATE_LIMIT_WINDOW = 60
RATE_LIMIT_MAX    = 20


def _is_rate_limited(ip: str) -> bool:
    now = time.time()
    with _rate_lock:
        ts = _rate_counters[ip]
        _rate_counters[ip] = [t for t in ts if now - t < RATE_LIMIT_WINDOW]
        if len(_rate_counters[ip]) >= RATE_LIMIT_MAX:
            return True
        _rate_counters[ip].append(now)
        return False

# ---------------------------------------------------------------------------
# CLEANUP PERIODICO
# ---------------------------------------------------------------------------

async def _cleanup_expired() -> None:
    while True:
        await asyncio.sleep(600)
        now = time.time()

        expired_tokens = [t for t, exp in active_tokens.items() if exp <= now]
        for t in expired_tokens:
            active_tokens.pop(t, None)

        expired_codes = [c for c, data in auth_codes.items() if data["expires"] <= now]
        for c in expired_codes:
            auth_codes.pop(c, None)

        db_deleted = _db_token_cleanup(now)

        if expired_tokens or expired_codes or db_deleted:
            log.info(
                "Cleanup: rimossi %d token (memoria), %d codici, %d token (DB)",
                len(expired_tokens), len(expired_codes), db_deleted,
            )

# ---------------------------------------------------------------------------
# PKCE
# ---------------------------------------------------------------------------

def verify_pkce(code_verifier: str, code_challenge: str) -> bool:
    digest   = hashlib.sha256(code_verifier.encode()).digest()
    computed = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return computed == code_challenge

# ---------------------------------------------------------------------------
# ENDPOINT OAUTH
# ---------------------------------------------------------------------------

async def well_known(request):
    base = str(request.base_url).rstrip("/")
    return JSONResponse({
        "issuer": base,
        "authorization_endpoint": f"{base}/authorize",
        "token_endpoint": f"{base}/token",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "client_credentials"],
        "code_challenge_methods_supported": ["S256"],
    })


async def authorize(request):
    ip = request.client.host if request.client else "unknown"
    if _is_rate_limited(ip):
        log.warning("Rate limit superato per IP=%s su /authorize", ip)
        return Response("Troppe richieste", status_code=429)

    params         = dict(request.query_params)
    client_id      = params.get("client_id")
    redirect_uri   = params.get("redirect_uri")
    code_challenge = params.get("code_challenge")
    state          = params.get("state", "")

    if client_id != OAUTH_CLIENT_ID:
        log.warning("Tentativo di autorizzazione con client_id non valido: %s", client_id)
        return Response("Client non autorizzato", status_code=401)

    code = secrets.token_hex(32)
    auth_codes[code] = {
        "code_challenge": code_challenge,
        "redirect_uri":   redirect_uri,
        "expires":        time.time() + 300,
    }

    log.info("Codice di autorizzazione emesso per redirect_uri=%s", redirect_uri)
    return RedirectResponse(
        url=f"{redirect_uri}?code={code}&state={state}",
        status_code=302,
    )


async def token_endpoint(request):
    ip = request.client.host if request.client else "unknown"
    if _is_rate_limited(ip):
        log.warning("Rate limit superato per IP=%s su /token", ip)
        return Response("Troppe richieste", status_code=429)

    form = await request.form()

    client_id     = form.get("client_id")
    client_secret = form.get("client_secret")

    auth_header = request.headers.get("authorization", "")
    if auth_header.startswith("Basic "):
        decoded = base64.b64decode(auth_header[6:]).decode()
        client_id, _, client_secret = decoded.partition(":")

    grant_type = form.get("grant_type")

    if grant_type == "authorization_code":
        code          = form.get("code")
        code_verifier = form.get("code_verifier")
        redirect_uri  = form.get("redirect_uri")

        if client_id != OAUTH_CLIENT_ID:
            log.warning("Token request con client_id non valido: %s", client_id)
            return JSONResponse({"error": "invalid_client"}, status_code=401)

        if client_secret and client_secret != OAUTH_CLIENT_SECRET:
            log.warning("Token request con client_secret errato per client_id=%s", client_id)
            return JSONResponse({"error": "invalid_client"}, status_code=401)

        stored = auth_codes.pop(code, None)
        if not stored:
            log.warning("Codice di autorizzazione non trovato o già usato")
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        if time.time() > stored["expires"]:
            log.warning("Codice di autorizzazione scaduto")
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        if stored["redirect_uri"] != redirect_uri:
            log.warning(
                "redirect_uri non corrisponde: atteso=%s ricevuto=%s",
                stored["redirect_uri"], redirect_uri,
            )
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        if not verify_pkce(code_verifier, stored["code_challenge"]):
            log.warning("Verifica PKCE fallita")
            return JSONResponse({"error": "invalid_grant"}, status_code=400)

        token   = secrets.token_hex(32)
        expires = time.time() + TOKEN_EXPIRY
        active_tokens[token] = expires
        _db_token_save(token, expires)
        log.info("Token emesso per client_id=%s (scade tra %ss)", client_id, TOKEN_EXPIRY)
        return JSONResponse({
            "access_token": token,
            "token_type":   "bearer",
            "expires_in":   TOKEN_EXPIRY,
        })

    if grant_type == "client_credentials":
        if client_id != OAUTH_CLIENT_ID or client_secret != OAUTH_CLIENT_SECRET:
            log.warning("Client credentials non valide per client_id=%s", client_id)
            return JSONResponse({"error": "invalid_client"}, status_code=401)
        token   = secrets.token_hex(32)
        expires = time.time() + TOKEN_EXPIRY
        active_tokens[token] = expires
        _db_token_save(token, expires)
        log.info("Token (client_credentials) emesso per client_id=%s", client_id)
        return JSONResponse({
            "access_token": token,
            "token_type":   "bearer",
            "expires_in":   TOKEN_EXPIRY,
        })

    return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)

# ---------------------------------------------------------------------------
# MIDDLEWARE DI AUTENTICAZIONE
# ---------------------------------------------------------------------------

class BearerTokenMiddleware(BaseHTTPMiddleware):
    PERCORSI_PUBBLICI = {
        "/authorize",
        "/token",
        "/.well-known/oauth-authorization-server",
        "/.well-known/oauth-protected-resource",
    }

    async def dispatch(self, request, call_next):
        if request.url.path in self.PERCORSI_PUBBLICI:
            return await call_next(request)
        if request.method == "OPTIONS":
            return await call_next(request)

        auth_header = request.headers.get("authorization", "")
        if not auth_header.startswith("Bearer "):
            log.warning("Richiesta senza Bearer token: %s %s", request.method, request.url.path)
            return Response("Non autorizzato", status_code=401)

        token  = auth_header[7:]
        expiry = active_tokens.get(token)

        if expiry is None:
            log.warning(
                "Token sconosciuto presentato da %s",
                request.client.host if request.client else "IP sconosciuto",
            )
            return Response("Token non valido o scaduto", status_code=401)

        if time.time() > expiry:
            active_tokens.pop(token, None)
            _db_token_delete(token)
            log.warning("Token scaduto presentato")
            return Response("Token non valido o scaduto", status_code=401)

        return await call_next(request)

# ---------------------------------------------------------------------------
# UTILITA' CONDIVISE
# ---------------------------------------------------------------------------

def _risolvi_path(path: str) -> tuple[str, str | None]:
    base  = os.path.realpath(BASE_DIR)
    clean = path.strip("/").strip()
    full  = os.path.realpath(os.path.join(BASE_DIR, clean)) if clean else base
    if not full.startswith(base):
        return "", "Accesso negato: usa solo percorsi relativi visibili nell'output di get_structure o list_all."
    return full, None


def _estrai_testo(full_path: str, max_chars: int = 200_000) -> str:
    """
    Estrae il testo da un file (PDF, DOCX, XLSX, testo semplice).
    Il troncamento a max_chars avviene internamente per evitare di
    caricare in memoria file molto grandi prima di tagliarli.
    """
    ext = Path(full_path).suffix.lower()

    if ext == ".pdf":
        try:
            import fitz
            doc    = fitz.open(full_path)
            pagine = []
            totale = 0
            for i, pagina in enumerate(doc, 1):
                testo = pagina.get_text("text")
                if not testo.strip():
                    continue
                h = f"--- Pagina {i} ---\n"
                pagine.append(h + testo)
                totale += len(h) + len(testo)
                if totale >= max_chars:
                    break
            doc.close()
            return "\n\n".join(pagine) if pagine else "[PDF senza testo estraibile: potrebbe essere scansionato]"
        except fitz.FileDataError:
            return "[PDF protetto da password o danneggiato: impossibile aprire il file]"
        except Exception as e:
            return f"[Errore lettura PDF: {e}]"

    if ext == ".docx":
        try:
            from docx import Document
            from docx.oxml.ns import qn

            doc    = Document(full_path)
            righe: list[str] = []
            totale = 0

            def _aggiungi(testo: str) -> bool:
                nonlocal totale
                if not testo.strip():
                    return True
                righe.append(testo)
                totale += len(testo)
                return totale < max_chars

            for section in doc.sections:
                for hf in [section.header, section.footer,
                           section.even_page_header, section.even_page_footer,
                           section.first_page_header, section.first_page_footer]:
                    if hf is None:
                        continue
                    try:
                        if hf.is_linked_to_previous:
                            continue
                    except Exception:
                        pass
                    for p in hf.paragraphs:
                        if not _aggiungi(p.text):
                            return "\n".join(righe)

            for p in doc.paragraphs:
                if not _aggiungi(p.text):
                    return "\n".join(righe)

            for tabella in doc.tables:
                for riga in tabella.rows:
                    if not _aggiungi("\t".join(c.text for c in riga.cells)):
                        return "\n".join(righe)

            try:
                for txbx in doc.element.body.iter(qn("w:txbxContent")):
                    for p in txbx.iter(qn("w:p")):
                        testo_p = "".join(t.text for t in p.iter(qn("w:t")) if t.text)
                        if not _aggiungi("[TextBox] " + testo_p):
                            return "\n".join(righe)
            except Exception:
                pass

            try:
                fp_part = doc.part.footnotes
                if fp_part is not None:
                    for fn in fp_part._element.iter(qn("w:footnote")):
                        fn_id = fn.get(qn("w:id"), "")
                        if fn_id in ("-1", "0"):
                            continue
                        testi = [t.text for t in fn.iter(qn("w:t")) if t.text]
                        if testi:
                            if not _aggiungi("[Nota] " + "".join(testi)):
                                return "\n".join(righe)
            except Exception:
                pass

            return "\n".join(righe) if righe else "[Documento DOCX vuoto]"
        except Exception as e:
            return f"[Errore lettura DOCX: {e}]"

    if ext in (".xlsx", ".xls"):
        try:
            import openpyxl
            wb     = openpyxl.load_workbook(full_path, read_only=True, data_only=True)
            righe  = []
            totale = 0
            for nome in wb.sheetnames:
                foglio = wb[nome]
                h = f"=== Foglio: {nome} ==="
                righe.append(h)
                totale += len(h)
                for riga in foglio.iter_rows(values_only=True):
                    if any(c is not None for c in riga):
                        r = "\t".join(str(c) if c is not None else "" for c in riga)
                        righe.append(r)
                        totale += len(r)
                        if totale >= max_chars:
                            return "\n".join(righe)
            return "\n".join(righe) if righe else "[File XLSX vuoto]"
        except Exception as e:
            return f"[Errore lettura XLSX: {e}]"

    try:
        with open(full_path, "r", encoding="utf-8", errors="replace") as f:
            return f.read(max_chars)
    except Exception as e:
        return f"[Errore lettura file: {e}]"


def _dimensione_leggibile(n_bytes: int) -> str:
    for unita in ("B", "KB", "MB", "GB"):
        if n_bytes < 1024:
            return f"{n_bytes:.0f} {unita}"
        n_bytes /= 1024
    return f"{n_bytes:.1f} GB"

# ---------------------------------------------------------------------------
# MCP — DEFINIZIONE SERVER
# ---------------------------------------------------------------------------

mcp = FastMCP(
    "FileServer Aziendale",
    streamable_http_path="/",
    host=ALLOWED_HOST,
)

# ---------------------------------------------------------------------------
# PROMPT — istruzioni permanenti per Claude
# ---------------------------------------------------------------------------

@mcp.prompt()
def istruzioni_file_server() -> str:
    return """
Hai accesso a un file server aziendale tramite cinque strumenti:
get_structure, list_all, read_file, search_files e search_content.

FLUSSO CONSIGLIATO:
1. Inizia SEMPRE con get_structure per avere una mappa leggera delle
   cartelle disponibili, a meno che l'utente non indichi già il file.
2. Se cerchi un file di cui conosci parte del nome, usa search_files.
3. Se cerchi informazioni senza sapere in quale file si trovano,
   usa search_content.
4. Usa list_all su una cartella specifica per vedere i file in essa.
5. Leggi i file con read_file usando i percorsi mostrati dagli altri tool.

REGOLE SUI PERCORSI:
- Usa sempre percorsi RELATIVI: "contratti", "2024/fatture/marzo.pdf".
- Non usare mai "/", ".", "..", "home", "root" o percorsi assoluti.
- I percorsi validi sono solo quelli mostrati da get_structure e list_all.

COMPORTAMENTO CON L'UTENTE:
- Prima di rispondere a domande su documenti, cerca e leggi i file rilevanti.
- Comunica in italiano semplice senza esporre dettagli tecnici degli strumenti.
- Se un file è troncato, aumenta max_chars in read_file o avvisa l'utente.
- Se non trovi un file con search_files, prova search_content con parole
  chiave diverse o più generiche.
- Se search_content avvisa che l'indice è ancora in costruzione, informa
  l'utente e suggerisci di riprovare tra qualche minuto.
"""

# ---------------------------------------------------------------------------
# TOOL — get_structure
# ---------------------------------------------------------------------------

@mcp.tool()
def get_structure() -> str:
    """
    Restituisce la mappa delle CARTELLE del file server, senza elencare i file.

    QUANDO USARLO: come primissimo passo prima di qualsiasi altra operazione,
    per capire come sono organizzati i documenti. La risposta è leggera e
    veloce anche su archivi molto grandi.

    Non richiede parametri.

    OUTPUT: albero delle cartelle con indentazione. Ogni riga mostra una
    cartella e il numero di file che contiene direttamente.
    Usa i nomi di cartella mostrati qui come parametro path in list_all.
    """
    base  = os.path.realpath(BASE_DIR)
    righe = []

    for root, dirs, files in os.walk(base):
        dirs.sort()
        livello   = os.path.relpath(root, base).count(os.sep)
        indent    = "  " * livello
        nome      = os.path.basename(root) if livello > 0 else "."
        n_file    = len(files)
        etichetta = f"  ({n_file} file)" if n_file else ""
        righe.append(f"{indent}{nome}/{etichetta}")

    totale_file = sum(len(f) for _, _, f in os.walk(base))
    righe.append(f"\nTotale file: {totale_file}")
    log.info("get_structure: %d righe restituite", len(righe))
    return "\n".join(righe)

# ---------------------------------------------------------------------------
# TOOL — list_all
# ---------------------------------------------------------------------------

@mcp.tool()
def list_all(path: str = "") -> str:
    """
    Elenca file e cartelle dentro una directory specifica del file server.

    QUANDO USARLO: dopo get_structure, per vedere i file dentro una cartella
    di interesse. Non usarlo sulla radice se ci sono molte cartelle: usa prima
    get_structure per orientarti, poi list_all su una sottocartella.

    COME USARLO:
    - path: nome della cartella da esplorare, relativo alla radice del server
      (es. "contratti" oppure "2024/fatture"). Lascia vuoto per vedere tutto.
    - Usa solo percorsi mostrati da get_structure. Non inventare percorsi.

    OUTPUT: albero con indentazione. I percorsi relativi dei file sono quelli
    da passare a read_file. Ogni file mostra anche dimensione e data di modifica.
    """
    full_path, errore = _risolvi_path(path)
    if errore:
        log.warning("list_all: accesso negato per path=%s", path)
        return errore
    if not os.path.exists(full_path):
        log.warning("list_all: percorso non trovato: %s", path)
        return (
            f"Percorso non trovato: '{path}'. "
            "Usa get_structure per vedere le cartelle disponibili."
        )

    righe         = []
    n_file_totali = 0

    for root, dirs, files in os.walk(full_path):
        dirs.sort()
        files.sort()
        livello       = os.path.relpath(root, full_path).count(os.sep)
        indent        = "  " * livello
        nome_cartella = os.path.basename(root) if livello > 0 else (path.strip("/") or ".")
        righe.append(f"{indent}{nome_cartella}/")
        for nome_file in files:
            fp = os.path.join(root, nome_file)
            try:
                stat = os.stat(fp)
                dim  = _dimensione_leggibile(stat.st_size)
                data = time.strftime("%d/%m/%Y", time.localtime(stat.st_mtime))
                info = f"  [{dim}, {data}]"
            except OSError:
                info = ""
            righe.append(f"{indent}  {nome_file}{info}")
            n_file_totali += 1

    righe.append(f"\nTotale file: {n_file_totali}")
    log.info("list_all: %d voci per path=%r", len(righe), path)
    return "\n".join(righe)

# ---------------------------------------------------------------------------
# TOOL — read_file
# ---------------------------------------------------------------------------

@mcp.tool()
def read_file(path: str, max_chars: int = 50_000) -> str:
    """
    Legge il contenuto di un file aziendale.
    Supporta PDF, DOCX, XLSX e testo semplice (txt, md, csv, json, xml, html).

    QUANDO USARLO: dopo aver identificato il file tramite list_all,
    get_structure, search_files o search_content.

    COME USARLO:
    - path: percorso relativo del file ESATTAMENTE come mostrato dagli altri
      strumenti (es. "contratti/fornitore_x.pdf" oppure "report.xlsx").
      Non inventare percorsi: usa solo quelli visti negli altri strumenti.
    - max_chars: numero massimo di caratteri restituiti (default: 50000).
      Se vedi [TRONCATO] alla fine, richiama con un valore più alto.
    - Puoi chiamare questo strumento più volte per leggere file diversi.
    """
    full_path, errore = _risolvi_path(path)
    if errore:
        log.warning("read_file: accesso negato per path=%s", path)
        return errore

    if not os.path.isfile(full_path):
        log.warning("read_file: file non trovato: %s", path)
        return (
            f"File non trovato: '{path}'. "
            "Usa list_all o search_files per trovare il percorso corretto."
        )

    ext = Path(full_path).suffix.lower()
    if ext not in ESTENSIONI_LEGGIBILI:
        log.warning("read_file: estensione non supportata: %s", ext)
        return (
            f"Il file '{path}' ha estensione '{ext}' non supportata. "
            "Formati leggibili: " + ", ".join(sorted(ESTENSIONI_LEGGIBILI))
        )

    try:
        stat = os.stat(full_path)
        dim  = _dimensione_leggibile(stat.st_size)
        data = time.strftime("%d/%m/%Y %H:%M", time.localtime(stat.st_mtime))
    except OSError:
        dim, data = "?", "?"

    log.info("read_file: lettura %s (ext=%s, dim=%s, max_chars=%d)", path, ext, dim, max_chars)

    intestazione = f"[File: {path} | Dimensione: {dim} | Ultima modifica: {data}]\n\n"
    risultato    = _estrai_testo(full_path, max_chars=max_chars + 1)

    if len(risultato) > max_chars:
        log.info("read_file: troncato a %d caratteri", max_chars)
        return (
            intestazione + risultato[:max_chars]
            + f"\n\n[TRONCATO: il file supera {max_chars} caratteri. "
            f"Richiama con max_chars più alto per leggere il resto.]"
        )

    return intestazione + risultato

# ---------------------------------------------------------------------------
# TOOL — search_files
# ---------------------------------------------------------------------------

@mcp.tool()
def search_files(keyword: str) -> str:
    """
    Cerca file il cui NOME contiene una parola chiave.

    QUANDO USARLO: quando conosci parte del nome del file che cerchi
    (es. "contratto_rossi", "fattura_2024", "bilancio").
    Se non conosci il nome ma sai cosa c'è scritto dentro, usa search_content.

    COME USARLO:
    - keyword: parola o parte di parola da cercare nel nome del file.
      La ricerca non distingue maiuscole e minuscole.

    OUTPUT: elenco di percorsi relativi da passare direttamente a read_file.
    """
    results = []
    for root, dirs, files in os.walk(BASE_DIR):
        for nome in sorted(files):
            if keyword.lower() in nome.lower():
                relativo = os.path.relpath(os.path.join(root, nome), BASE_DIR)
                results.append(relativo)

    log.info("search_files: keyword=%r -> %d risultati", keyword, len(results))
    if not results:
        return (
            f"Nessun file trovato con '{keyword}' nel nome. "
            "Prova con una parola chiave diversa o usa search_content per "
            "cercare nel contenuto dei file."
        )
    return f"File trovati ({len(results)}):\n" + "\n".join(results)

# ---------------------------------------------------------------------------
# TOOL — search_content
# ---------------------------------------------------------------------------

@mcp.tool()
def search_content(keyword: str, max_results: int = 10) -> str:
    """
    Cerca una parola o frase nel CONTENUTO dei file aziendali usando
    l'indice full-text persistente. La ricerca è istantanea indipendentemente
    dal numero e dalla dimensione dei file.

    QUANDO USARLO: quando l'utente cerca informazioni senza sapere in quale
    file si trovano (es. "trova il contratto che menziona la penale",
    "cerca il documento con il codice fiscale di Rossi").

    COME USARLO:
    - keyword: parola o frase da cercare nel testo.
      Supporta operatori FTS5: AND, OR, NOT, "frase esatta", prefisso*.
      Esempi: "penale contratto", "rossi AND fattura", "pag*"
    - max_results: numero massimo di file da restituire (default: 10).

    OUTPUT: per ogni file trovato mostra il percorso relativo e un estratto
    del contesto attorno alla parola cercata (la parola trovata è tra []).
    Usa i percorsi restituiti con read_file per leggere i file completi.

    NOTA: se l'indice è ancora in costruzione al primo avvio del server,
    i risultati potrebbero essere parziali. Lo stato è visibile nell'output.
    """
    avviso = ""
    if _index_state["running"]:
        done  = _index_state["done"]
        total = _index_state["total"]
        avviso = (
            f"[Indice in costruzione: {done}/{total} file elaborati. "
            f"I risultati potrebbero essere parziali.]\n\n"
        )

    try:
        rows = _db_search(keyword, max_results)
    except Exception as e:
        log.warning("search_content: errore query FTS per keyword=%r: %s", keyword, e)
        return (
            f"Sintassi di ricerca non valida: {e}\n"
            "Usa parole semplici o operatori FTS5 validi: AND, OR, NOT, "
            "\"frase esatta\", prefisso*."
        )

    log.info("search_content: keyword=%r -> %d risultati", keyword, len(rows))

    if not rows:
        n_totale = _db_index_count()
        return (
            avviso +
            f"Nessun file contiene '{keyword}' "
            f"(indice: {n_totale} file). "
            "Prova con una parola chiave più generica o verifica l'ortografia."
        )

    n_totale    = _db_index_count()
    intestazione = f"File che contengono '{keyword}' ({len(rows)} trovati"
    if len(rows) == max_results:
        intestazione += f", mostrati i primi {max_results}"
    intestazione += f", su {n_totale} file nell'indice):\n\n"

    righe = [f"{path}\n  contesto: {estratto}" for path, estratto in rows]
    return avviso + intestazione + "\n\n".join(righe)

# ---------------------------------------------------------------------------
# AVVIO
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn

    _db_init()
    active_tokens.update(_db_load_active_tokens())
    log.info("Token attivi ricaricati dal DB: %d", len(active_tokens))

    # Indicizzazione iniziale in background: non blocca l'avvio del server.
    # Al primo avvio legge tutti i file; ai riavvii successivi indicizza
    # solo i file cambiati dall'ultima volta (molto piu' veloce).
    _start_indexing_thread()

    # Watcher in tempo reale: aggiorna l'indice quando i file cambiano.
    _start_file_watcher()

    @asynccontextmanager
    async def lifespan(app):
        cleanup_task = asyncio.create_task(_cleanup_expired())
        log.info("Task di cleanup token avviato")
        async with mcp.session_manager.run():
            log.info("MCP session manager avviato")
            yield
        cleanup_task.cancel()
        log.info("MCP session manager e cleanup fermati")

    routes = [
        Route("/.well-known/oauth-authorization-server", well_known,    methods=["GET"]),
        Route("/.well-known/oauth-protected-resource",   well_known,    methods=["GET"]),
        Route("/authorize",                              authorize,      methods=["GET"]),
        Route("/token",                                  token_endpoint, methods=["POST"]),
        Mount("/", app=mcp.streamable_http_app()),
    ]

    app = Starlette(routes=routes, lifespan=lifespan)
    app.add_middleware(BearerTokenMiddleware)

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=443,
        ssl_certfile=SSL_CERTFILE,
        ssl_keyfile=SSL_KEYFILE,
    )
    ```