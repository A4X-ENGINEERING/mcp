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


BASE_DIR = _require_env("MCP_BASE_DIR")
OAUTH_CLIENT_ID = _require_env("MCP_CLIENT_ID")
OAUTH_CLIENT_SECRET = _require_env("MCP_CLIENT_SECRET")
ALLOWED_HOST = _require_env("MCP_ALLOWED_HOST")

TOKEN_EXPIRY = int(os.environ.get("MCP_TOKEN_EXPIRY", "86400"))
DB_PATH = os.environ.get("MCP_DB_PATH", "/opt/mcp-fileserver/mcp.db")

log.info(
    "BASE_DIR=%s ALLOWED_HOST=%s TOKEN_EXPIRY=%s DB_PATH=%s MODE=catalog",
    BASE_DIR,
    ALLOWED_HOST,
    TOKEN_EXPIRY,
    DB_PATH,
)

# ---------------------------------------------------------------------------
# DATABASE — INIT E LOCK (solo tokens)
# ---------------------------------------------------------------------------


_db_write_lock = threading.Lock()


def _db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def _db_init() -> None:
    with _db_write_lock:
        conn = _db_connect()
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS tokens (
                token   TEXT PRIMARY KEY,
                expires REAL NOT NULL
            );
            """
        )
        conn.commit()
        conn.close()


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
        count = conn.execute("DELETE FROM tokens WHERE expires <= ?", (now,)).rowcount
        conn.commit()
        conn.close()
    return count


def _db_load_active_tokens() -> dict[str, float]:
    now = time.time()
    conn = _db_connect()
    rows = conn.execute(
        "SELECT token, expires FROM tokens WHERE expires > ?",
        (now,),
    ).fetchall()
    conn.close()
    return {token: expires for token, expires in rows}


# ---------------------------------------------------------------------------
# STATO IN MEMORIA
# ---------------------------------------------------------------------------


auth_codes: dict[str, dict] = {}
active_tokens: dict[str, float] = {}

# ---------------------------------------------------------------------------
# RATE LIMITING SUGLI ENDPOINT PUBBLICI
# ---------------------------------------------------------------------------


_rate_lock = threading.Lock()
_rate_counters: dict[str, list[float]] = defaultdict(list)

RATE_LIMIT_WINDOW = 60
RATE_LIMIT_MAX = 20


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
                len(expired_tokens),
                len(expired_codes),
                db_deleted,
            )


# ---------------------------------------------------------------------------
# PKCE
# ---------------------------------------------------------------------------


def verify_pkce(code_verifier: str, code_challenge: str) -> bool:
    digest = hashlib.sha256(code_verifier.encode()).digest()
    computed = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return computed == code_challenge


# ---------------------------------------------------------------------------
# ENDPOINT OAUTH
# ---------------------------------------------------------------------------


async def well_known(request):
    base = str(request.base_url).rstrip("/")
    return JSONResponse(
        {
            "issuer": base,
            "authorization_endpoint": f"{base}/authorize",
            "token_endpoint": f"{base}/token",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "client_credentials"],
            "code_challenge_methods_supported": ["S256"],
        }
    )


async def authorize(request):
    ip = request.client.host if request.client else "unknown"
    if _is_rate_limited(ip):
        log.warning("Rate limit superato per IP=%s su /authorize", ip)
        return Response("Troppe richieste", status_code=429)

    params = dict(request.query_params)
    client_id = params.get("client_id")
    redirect_uri = params.get("redirect_uri")
    code_challenge = params.get("code_challenge")
    state = params.get("state", "")

    if client_id != OAUTH_CLIENT_ID:
        log.warning("Tentativo di autorizzazione con client_id non valido: %s", client_id)
        return Response("Client non autorizzato", status_code=401)

    code = secrets.token_hex(32)
    auth_codes[code] = {
        "code_challenge": code_challenge,
        "redirect_uri": redirect_uri,
        "expires": time.time() + 300,
    }

    log.info("Codice di autorizzazione emesso per redirect_uri=%s", redirect_uri)
    return RedirectResponse(url=f"{redirect_uri}?code={code}&state={state}", status_code=302)


async def token_endpoint(request):
    ip = request.client.host if request.client else "unknown"
    if _is_rate_limited(ip):
        log.warning("Rate limit superato per IP=%s su /token", ip)
        return Response("Troppe richieste", status_code=429)

    form = await request.form()

    client_id = form.get("client_id")
    client_secret = form.get("client_secret")

    auth_header = request.headers.get("authorization", "")
    if auth_header.startswith("Basic "):
        decoded = base64.b64decode(auth_header[6:]).decode()
        client_id, _, client_secret = decoded.partition(":")

    grant_type = form.get("grant_type")

    if grant_type == "authorization_code":
        code = form.get("code")
        code_verifier = form.get("code_verifier")
        redirect_uri = form.get("redirect_uri")

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
                stored["redirect_uri"],
                redirect_uri,
            )
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        if not verify_pkce(code_verifier, stored["code_challenge"]):
            log.warning("Verifica PKCE fallita")
            return JSONResponse({"error": "invalid_grant"}, status_code=400)

        token = secrets.token_hex(32)
        expires = time.time() + TOKEN_EXPIRY
        active_tokens[token] = expires
        _db_token_save(token, expires)
        log.info("Token emesso per client_id=%s (scade tra %ss)", client_id, TOKEN_EXPIRY)
        return JSONResponse(
            {"access_token": token, "token_type": "bearer", "expires_in": TOKEN_EXPIRY}
        )

    if grant_type == "client_credentials":
        if client_id != OAUTH_CLIENT_ID or client_secret != OAUTH_CLIENT_SECRET:
            log.warning("Client credentials non valide per client_id=%s", client_id)
            return JSONResponse({"error": "invalid_client"}, status_code=401)

        token = secrets.token_hex(32)
        expires = time.time() + TOKEN_EXPIRY
        active_tokens[token] = expires
        _db_token_save(token, expires)
        log.info("Token (client_credentials) emesso per client_id=%s", client_id)
        return JSONResponse(
            {"access_token": token, "token_type": "bearer", "expires_in": TOKEN_EXPIRY}
        )

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

        token = auth_header[7:]
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
# UTILITA'
# ---------------------------------------------------------------------------


def _risolvi_path(path: str) -> tuple[str, str | None]:
    base = os.path.realpath(BASE_DIR)
    clean = path.strip("/").strip()
    full = os.path.realpath(os.path.join(BASE_DIR, clean)) if clean else base
    if not full.startswith(base):
        return "", "Accesso negato: usa solo percorsi relativi visibili nell'output di get_structure o list_all."
    return full, None


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
    "FileServer Aziendale (Catalogo)",
    streamable_http_path="/",
    host=ALLOWED_HOST,
)


@mcp.prompt()
def istruzioni_file_server() -> str:
    return """
Hai accesso a un catalogo documentale aziendale tramite tre strumenti:
get_structure, list_all e search_files.

OBIETTIVO:
- Capire velocemente come sono organizzati i documenti (cartelle e nomi file)
  senza leggere il contenuto.

FLUSSO CONSIGLIATO:
1. Inizia SEMPRE con get_structure per una mappa leggera delle cartelle.
2. Usa list_all su una cartella specifica per vedere file e sottocartelle.
3. Se conosci parte del nome del file, usa search_files.

REGOLE SUI PERCORSI:
- Usa sempre percorsi RELATIVI (es. "contratti" oppure "2024/fatture").
- Non usare percorsi assoluti o ..
"""


# ---------------------------------------------------------------------------
# TOOL — get_structure
# ---------------------------------------------------------------------------


@mcp.tool()
def get_structure() -> str:
    """Restituisce la mappa delle CARTELLE del file server, senza elencare i file."""
    base = os.path.realpath(BASE_DIR)
    righe = []

    for root, dirs, files in os.walk(base):
        dirs.sort()
        livello = os.path.relpath(root, base).count(os.sep)
        indent = "  " * livello
        nome = os.path.basename(root) if livello > 0 else "."
        n_file = len(files)
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
    """Elenca file e cartelle dentro una directory specifica del file server."""
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

    righe: list[str] = []
    n_file_totali = 0

    for root, dirs, files in os.walk(full_path):
        dirs.sort()
        files.sort()
        livello = os.path.relpath(root, full_path).count(os.sep)
        indent = "  " * livello
        nome_cartella = os.path.basename(root) if livello > 0 else (path.strip("/") or ".")
        righe.append(f"{indent}{nome_cartella}/")
        for nome_file in files:
            fp = os.path.join(root, nome_file)
            try:
                stat = os.stat(fp)
                dim = _dimensione_leggibile(stat.st_size)
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
# TOOL — search_files (solo nomi)
# ---------------------------------------------------------------------------


@mcp.tool()
def search_files(keyword: str) -> str:
    """Cerca file il cui NOME contiene una parola chiave (case-insensitive)."""
    results: list[str] = []
    key = (keyword or "").strip().lower()
    if not key:
        return "Specifica una parola chiave non vuota."

    for root, dirs, files in os.walk(BASE_DIR):
        for nome in sorted(files):
            if nome.startswith("~$"):
                continue
            if key in nome.lower():
                relativo = os.path.relpath(os.path.join(root, nome), BASE_DIR)
                results.append(relativo)

    log.info("search_files: keyword=%r -> %d risultati", keyword, len(results))
    if not results:
        return f"Nessun file trovato con '{keyword}' nel nome. Prova con una parola chiave diversa."
    return f"File trovati ({len(results)}):\n" + "\n".join(results)


# ---------------------------------------------------------------------------
# AVVIO
# ---------------------------------------------------------------------------


if __name__ == "__main__":
    import uvicorn

    _db_init()
    active_tokens.update(_db_load_active_tokens())
    log.info("Token attivi ricaricati dal DB: %d", len(active_tokens))

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
        Route("/.well-known/oauth-authorization-server", well_known, methods=["GET"]),
        Route("/.well-known/oauth-protected-resource", well_known, methods=["GET"]),
        Route("/authorize", authorize, methods=["GET"]),
        Route("/token", token_endpoint, methods=["POST"]),
        Mount("/", app=mcp.streamable_http_app()),
    ]

    app = Starlette(routes=routes, lifespan=lifespan)
    app.add_middleware(BearerTokenMiddleware)

    uvicorn.run(app, host="127.0.0.1", port=8000)
