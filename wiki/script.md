```python
“””
MCP File Server Aziendale

Variabili d’ambiente richieste:
MCP_BASE_DIR        percorso della cartella documenti
MCP_CLIENT_ID       identificativo OAuth (es. mcp-aziendale)
MCP_CLIENT_SECRET   segreto OAuth (genera con: python3 -c “import secrets; print(secrets.token_hex(32))”)
MCP_ALLOWED_HOST    dominio pubblico (es. mcp.tuaazienda.com)
MCP_SSL_CERTFILE    percorso al certificato SSL (fullchain.pem)
MCP_SSL_KEYFILE     percorso alla chiave privata SSL (privkey.pem)

Variabili opzionali:
MCP_TOKEN_EXPIRY    durata token in secondi (default: 86400)
MCP_DB_PATH         percorso file SQLite per la persistenza dei token (default: /tmp/mcp-tokens.db)

Modifiche rispetto alla versione precedente:

- Sostituzione di pypdf con pymupdf: estrazione PDF più robusta su layout
  a colonne, tabelle complesse e file danneggiati o protetti.
- Estrazione DOCX estesa: intestazioni, piè di pagina, note a piè di pagina
  e caselle di testo (text boxes nel corpo XML).
- Correzione bug max_chars: il troncamento ora avviene dentro _estrai_testo,
  evitando di caricare in memoria l’intero contenuto del file prima di tagliarlo.
- Correzione bug search_content: il break interrompeva solo il ciclo interno;
  ora il controllo su max_results viene applicato anche al ciclo esterno.
- Persistenza token su SQLite: i token attivi sopravvivono al riavvio del
  processo; all’avvio i token validi vengono ricaricati in memoria.
- Rate limiting sugli endpoint pubblici OAuth (/authorize, /token): massimo
  20 richieste per IP in una finestra di 60 secondi.
- Limite dimensione file in search_content: i file più grandi di
  MAX_SEARCH_FILE_BYTES vengono saltati per evitare scansioni troppo lente.
  “””

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

# —————————————————————————

# LOGGING

# —————————————————————————

logging.basicConfig(
level=logging.INFO,
format=”%(asctime)s %(levelname)s %(name)s: %(message)s”,
datefmt=”%Y-%m-%dT%H:%M:%S”,
)
log = logging.getLogger(“mcp-fileserver”)

# —————————————————————————

# CONFIGURAZIONE DA VARIABILI D’AMBIENTE

# —————————————————————————

def _require_env(name: str) -> str:
value = os.environ.get(name)
if not value:
raise RuntimeError(
f”Variabile d’ambiente obbligatoria non impostata: {name}\n”
“Vedi il file /etc/mcp-fileserver/secrets”
)
return value

BASE_DIR            = _require_env(“MCP_BASE_DIR”)
OAUTH_CLIENT_ID     = _require_env(“MCP_CLIENT_ID”)
OAUTH_CLIENT_SECRET = _require_env(“MCP_CLIENT_SECRET”)
ALLOWED_HOST        = _require_env(“MCP_ALLOWED_HOST”)
SSL_CERTFILE        = _require_env(“MCP_SSL_CERTFILE”)
SSL_KEYFILE         = _require_env(“MCP_SSL_KEYFILE”)
TOKEN_EXPIRY        = int(os.environ.get(“MCP_TOKEN_EXPIRY”, “86400”))
DB_PATH             = os.environ.get(“MCP_DB_PATH”, “/tmp/mcp-tokens.db”)

# Dimensione massima (in byte) di un singolo file scansionato da search_content.

# File più grandi vengono saltati per evitare attese eccessive.

MAX_SEARCH_FILE_BYTES = 20 * 1024 * 1024  # 20 MB

log.info(
“BASE_DIR=%s ALLOWED_HOST=%s TOKEN_EXPIRY=%s DB_PATH=%s”,
BASE_DIR, ALLOWED_HOST, TOKEN_EXPIRY, DB_PATH,
)

# —————————————————————————

# PERSISTENZA TOKEN SU SQLITE

# —————————————————————————

def _db_init() -> None:
“”“Crea la tabella dei token se non esiste.”””
with sqlite3.connect(DB_PATH) as conn:
conn.execute(”””
CREATE TABLE IF NOT EXISTS tokens (
token   TEXT PRIMARY KEY,
expires REAL NOT NULL
)
“””)
conn.commit()

def _db_token_save(token: str, expires: float) -> None:
with sqlite3.connect(DB_PATH) as conn:
conn.execute(
“INSERT OR REPLACE INTO tokens (token, expires) VALUES (?, ?)”,
(token, expires),
)
conn.commit()

def _db_token_delete(token: str) -> None:
with sqlite3.connect(DB_PATH) as conn:
conn.execute(“DELETE FROM tokens WHERE token = ?”, (token,))
conn.commit()

def _db_token_cleanup(now: float) -> int:
with sqlite3.connect(DB_PATH) as conn:
count = conn.execute(
“DELETE FROM tokens WHERE expires <= ?”, (now,)
).rowcount
conn.commit()
return count

def _db_load_active_tokens() -> dict[str, float]:
“”“Carica in memoria tutti i token non ancora scaduti all’avvio.”””
now = time.time()
with sqlite3.connect(DB_PATH) as conn:
rows = conn.execute(
“SELECT token, expires FROM tokens WHERE expires > ?”, (now,)
).fetchall()
return {token: expires for token, expires in rows}

# —————————————————————————

# STATO IN MEMORIA

# —————————————————————————

auth_codes: dict[str, dict] = {}
active_tokens: dict[str, float] = {}   # token -> scadenza (epoch); ricaricato dal DB all’avvio

# —————————————————————————

# RATE LIMITING SUGLI ENDPOINT PUBBLICI

# —————————————————————————

_rate_lock = threading.Lock()
_rate_counters: dict[str, list[float]] = defaultdict(list)

RATE_LIMIT_WINDOW = 60   # secondi
RATE_LIMIT_MAX    = 20   # richieste per finestra per IP

def _is_rate_limited(ip: str) -> bool:
“””
Restituisce True se l’IP ha superato il limite.
Usa una finestra scorrevole di RATE_LIMIT_WINDOW secondi.
“””
now = time.time()
with _rate_lock:
ts = _rate_counters[ip]
_rate_counters[ip] = [t for t in ts if now - t < RATE_LIMIT_WINDOW]
if len(_rate_counters[ip]) >= RATE_LIMIT_MAX:
return True
_rate_counters[ip].append(now)
return False

# —————————————————————————

# CLEANUP PERIODICO

# —————————————————————————

async def _cleanup_expired() -> None:
“”“Rimuove ogni 10 minuti i token e i codici scaduti.”””
while True:
await asyncio.sleep(600)
now = time.time()

```
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
```

# —————————————————————————

# PKCE

# —————————————————————————

def verify_pkce(code_verifier: str, code_challenge: str) -> bool:
digest   = hashlib.sha256(code_verifier.encode()).digest()
computed = base64.urlsafe_b64encode(digest).rstrip(b”=”).decode()
return computed == code_challenge

# —————————————————————————

# ENDPOINT OAUTH

# —————————————————————————

async def well_known(request):
base = str(request.base_url).rstrip(”/”)
return JSONResponse({
“issuer”: base,
“authorization_endpoint”: f”{base}/authorize”,
“token_endpoint”: f”{base}/token”,
“response_types_supported”: [“code”],
“grant_types_supported”: [“authorization_code”, “client_credentials”],
“code_challenge_methods_supported”: [“S256”],
})

async def authorize(request):
ip = request.client.host if request.client else “unknown”
if _is_rate_limited(ip):
log.warning(“Rate limit superato per IP=%s su /authorize”, ip)
return Response(“Troppe richieste”, status_code=429)

```
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
```

async def token_endpoint(request):
ip = request.client.host if request.client else “unknown”
if _is_rate_limited(ip):
log.warning(“Rate limit superato per IP=%s su /token”, ip)
return Response(“Troppe richieste”, status_code=429)

```
form = await request.form()

client_id     = form.get("client_id")
client_secret = form.get("client_secret")

# supporto Basic Auth oltre che form body
auth_header = request.headers.get("authorization", "")
if auth_header.startswith("Basic "):
    decoded = base64.b64decode(auth_header[6:]).decode()
    client_id, _, client_secret = decoded.partition(":")

grant_type = form.get("grant_type")

# --- Authorization Code + PKCE ---
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

    token    = secrets.token_hex(32)
    expires  = time.time() + TOKEN_EXPIRY
    active_tokens[token] = expires
    _db_token_save(token, expires)
    log.info("Token emesso per client_id=%s (scade tra %ss)", client_id, TOKEN_EXPIRY)
    return JSONResponse({
        "access_token": token,
        "token_type":   "bearer",
        "expires_in":   TOKEN_EXPIRY,
    })

# --- Client Credentials (fallback) ---
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
```

# —————————————————————————

# MIDDLEWARE DI AUTENTICAZIONE

# —————————————————————————

class BearerTokenMiddleware(BaseHTTPMiddleware):
PERCORSI_PUBBLICI = {
“/authorize”,
“/token”,
“/.well-known/oauth-authorization-server”,
“/.well-known/oauth-protected-resource”,
}

```
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
```

# —————————————————————————

# UTILITÀ CONDIVISE TRA I TOOL

# —————————————————————————

ESTENSIONI_LEGGIBILI = {
“.pdf”, “.docx”, “.xlsx”, “.xls”,
“.txt”, “.md”, “.csv”, “.json”, “.xml”, “.html”, “.htm”,
}

def _risolvi_path(path: str) -> tuple[str, str | None]:
“””
Risolve un percorso relativo in percorso assoluto verificando che resti
dentro BASE_DIR. Restituisce (percorso_assoluto, errore_o_None).
“””
base  = os.path.realpath(BASE_DIR)
clean = path.strip(”/”).strip()
full  = os.path.realpath(os.path.join(BASE_DIR, clean)) if clean else base
if not full.startswith(base):
return “”, “Accesso negato: usa solo percorsi relativi visibili nell’output di get_structure o list_all.”
return full, None

def _estrai_testo(full_path: str, max_chars: int = 200_000) -> str:
“””
Estrae il testo da un file (PDF, DOCX, XLSX, testo semplice).
Il parametro max_chars viene applicato internamente: la funzione
smette di accumulare testo non appena supera il limite.

```
FIX rispetto alla versione precedente:
- max_chars era dichiarato ma ignorato; ora è effettivamente usato.
- PDF: usa pymupdf (fitz) al posto di pypdf per una migliore estrazione
  su layout complessi e gestione esplicita di file protetti/danneggiati.
- DOCX: estrae anche intestazioni, piè di pagina, note a piè di pagina
  e caselle di testo presenti nel corpo XML del documento.
"""
ext = Path(full_path).suffix.lower()

# --- PDF tramite pymupdf ---
if ext == ".pdf":
    try:
        import fitz  # pymupdf
        doc    = fitz.open(full_path)
        pagine = []
        totale = 0
        for i, pagina in enumerate(doc, 1):
            testo = pagina.get_text("text")
            if not testo.strip():
                continue
            intestazione_pagina = f"--- Pagina {i} ---\n"
            pagine.append(intestazione_pagina + testo)
            totale += len(intestazione_pagina) + len(testo)
            if totale >= max_chars:
                break
        doc.close()
        return "\n\n".join(pagine) if pagine else "[PDF senza testo estraibile: potrebbe essere scansionato]"
    except fitz.FileDataError:
        return "[PDF protetto da password o danneggiato: impossibile aprire il file]"
    except Exception as e:
        return f"[Errore lettura PDF: {e}]"

# --- DOCX tramite python-docx con estrazione estesa ---
if ext == ".docx":
    try:
        from docx import Document
        from docx.oxml.ns import qn

        doc  = Document(full_path)
        righe: list[str] = []
        totale = 0

        def _aggiungi(testo: str) -> bool:
            """Aggiunge testo alla lista; restituisce False se il limite è raggiunto."""
            nonlocal totale
            if not testo.strip():
                return True
            righe.append(testo)
            totale += len(testo)
            return totale < max_chars

        # Intestazioni e piè di pagina
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

        # Paragrafi del corpo principale
        for p in doc.paragraphs:
            if not _aggiungi(p.text):
                return "\n".join(righe)

        # Tabelle
        for tabella in doc.tables:
            for riga in tabella.rows:
                riga_testo = "\t".join(c.text for c in riga.cells)
                if not _aggiungi(riga_testo):
                    return "\n".join(righe)

        # Caselle di testo (text boxes nel corpo XML)
        try:
            corpo = doc.element.body
            for txbx in corpo.iter(qn("w:txbxContent")):
                for p in txbx.iter(qn("w:p")):
                    testo_p = "".join(
                        t.text for t in p.iter(qn("w:t")) if t.text
                    )
                    if not _aggiungi("[TextBox] " + testo_p):
                        return "\n".join(righe)
        except Exception:
            pass

        # Note a piè di pagina
        try:
            fp_part = doc.part.footnotes
            if fp_part is not None:
                for fn in fp_part._element.iter(qn("w:footnote")):
                    fn_id = fn.get(qn("w:id"), "")
                    if fn_id in ("-1", "0"):
                        continue  # separatori
                    testi = [t.text for t in fn.iter(qn("w:t")) if t.text]
                    if testi:
                        if not _aggiungi("[Nota] " + "".join(testi)):
                            return "\n".join(righe)
        except Exception:
            pass

        return "\n".join(righe) if righe else "[Documento DOCX vuoto]"
    except Exception as e:
        return f"[Errore lettura DOCX: {e}]"

# --- XLSX / XLS tramite openpyxl ---
if ext in (".xlsx", ".xls"):
    try:
        import openpyxl
        wb    = openpyxl.load_workbook(full_path, read_only=True, data_only=True)
        righe = []
        totale = 0
        for nome in wb.sheetnames:
            foglio  = wb[nome]
            header  = f"=== Foglio: {nome} ==="
            righe.append(header)
            totale += len(header)
            for riga in foglio.iter_rows(values_only=True):
                if any(c is not None for c in riga):
                    riga_testo = "\t".join(str(c) if c is not None else "" for c in riga)
                    righe.append(riga_testo)
                    totale += len(riga_testo)
                    if totale >= max_chars:
                        return "\n".join(righe)
        return "\n".join(righe) if righe else "[File XLSX vuoto]"
    except Exception as e:
        return f"[Errore lettura XLSX: {e}]"

# --- Testo semplice (txt, md, csv, json, xml, html, ...) ---
try:
    with open(full_path, "r", encoding="utf-8", errors="replace") as f:
        return f.read(max_chars)
except Exception as e:
    return f"[Errore lettura file: {e}]"
```

def _dimensione_leggibile(n_bytes: int) -> str:
for unita in (“B”, “KB”, “MB”, “GB”):
if n_bytes < 1024:
return f”{n_bytes:.0f} {unita}”
n_bytes /= 1024
return f”{n_bytes:.1f} GB”

# —————————————————————————

# MCP — DEFINIZIONE SERVER

# —————————————————————————

mcp = FastMCP(
“FileServer Aziendale”,
streamable_http_path=”/”,
host=ALLOWED_HOST,
)

# —————————————————————————

# PROMPT — istruzioni permanenti per Claude

# —————————————————————————

@mcp.prompt()
def istruzioni_file_server() -> str:
“””
Istruzioni su come usare il file server aziendale.
Vengono caricate automaticamente all’avvio della sessione.
“””
return “””
Hai accesso a un file server aziendale tramite cinque strumenti:
get_structure, list_all, read_file, search_files e search_content.

FLUSSO CONSIGLIATO:

1. Inizia SEMPRE con get_structure per avere una mappa leggera delle
   cartelle disponibili, a meno che l’utente non indichi già il file.
2. Se cerchi un file di cui conosci parte del nome, usa search_files.
3. Se cerchi informazioni senza sapere in quale file si trovano,
   usa search_content.
4. Usa list_all su una cartella specifica per vedere i file in essa.
5. Leggi i file con read_file usando i percorsi mostrati dagli altri tool.

REGOLE SUI PERCORSI:

- Usa sempre percorsi RELATIVI: “contratti”, “2024/fatture/marzo.pdf”.
- Non usare mai “/”, “.”, “..”, “home”, “root” o percorsi assoluti.
- I percorsi validi sono solo quelli mostrati da get_structure e list_all.

COMPORTAMENTO CON L’UTENTE:

- Prima di rispondere a domande su documenti, cerca e leggi i file rilevanti.
- Comunica in italiano semplice senza esporre dettagli tecnici degli strumenti.
- Se un file è troncato, aumenta max_chars in read_file o avvisa l’utente.
- Se non trovi un file con search_files, prova search_content con parole
  chiave diverse o più generiche.
  “””

# —————————————————————————

# TOOL — get_structure

# —————————————————————————

@mcp.tool()
def get_structure() -> str:
“””
Restituisce la mappa delle CARTELLE del file server, senza elencare i file.

```
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
    livello = os.path.relpath(root, base).count(os.sep)
    indent  = "  " * livello
    nome    = os.path.basename(root) if livello > 0 else "."
    n_file  = len(files)
    etichetta = f"  ({n_file} file)" if n_file else ""
    righe.append(f"{indent}{nome}/{etichetta}")

totale_file = sum(len(f) for _, _, f in os.walk(base))
righe.append(f"\nTotale file: {totale_file}")
log.info("get_structure: %d righe restituite", len(righe))
return "\n".join(righe)
```

# —————————————————————————

# TOOL — list_all

# —————————————————————————

@mcp.tool()
def list_all(path: str = “”) -> str:
“””
Elenca file e cartelle dentro una directory specifica del file server.

```
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
    livello        = os.path.relpath(root, full_path).count(os.sep)
    indent         = "  " * livello
    nome_cartella  = os.path.basename(root) if livello > 0 else (path.strip("/") or ".")
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
```

# —————————————————————————

# TOOL — read_file

# —————————————————————————

@mcp.tool()
def read_file(path: str, max_chars: int = 50_000) -> str:
“””
Legge il contenuto di un file aziendale.
Supporta PDF, DOCX, XLSX e testo semplice (txt, md, csv, json, xml, html).

```
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

log.info(
    "read_file: lettura %s (ext=%s, dim=%s, max_chars=%d)",
    path, ext, dim, max_chars,
)

intestazione = f"[File: {path} | Dimensione: {dim} | Ultima modifica: {data}]\n\n"

# FIX: max_chars viene ora passato a _estrai_testo, che lo applica
# internamente. Questo evita di caricare in memoria l'intero contenuto
# del file solo per tagliarlo dopo.
# Viene richiesto max_chars + 1 per rilevare se il file è stato troncato.
risultato = _estrai_testo(full_path, max_chars=max_chars + 1)

if len(risultato) > max_chars:
    log.info(
        "read_file: troncato a %d caratteri (testo estratto: %d+)",
        max_chars, max_chars,
    )
    return (
        intestazione + risultato[:max_chars]
        + f"\n\n[TRONCATO: il file supera {max_chars} caratteri. "
        f"Richiama con max_chars più alto per leggere il resto.]"
    )

return intestazione + risultato
```

# —————————————————————————

# TOOL — search_files

# —————————————————————————

@mcp.tool()
def search_files(keyword: str) -> str:
“””
Cerca file il cui NOME contiene una parola chiave.

```
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
```

# —————————————————————————

# TOOL — search_content

# —————————————————————————

@mcp.tool()
def search_content(keyword: str, max_results: int = 10) -> str:
“””
Cerca una parola o frase nel CONTENUTO dei file aziendali.
Supporta PDF, DOCX, XLSX e file di testo.

```
QUANDO USARLO: quando l'utente cerca informazioni senza sapere in quale
file si trovano (es. "trova il contratto che menziona la penale",
"cerca il documento con il codice fiscale di Rossi").
È più lento di search_files perché apre i file: usalo dopo aver provato
search_files senza risultato, o quando la ricerca è per contenuto.

COME USARLO:
- keyword: parola o frase da cercare nel testo. Non distingue maiuscole.
- max_results: numero massimo di file da restituire (default: 10).

OUTPUT: per ogni file trovato mostra il percorso relativo e un estratto
del contesto attorno alla parola cercata.
Usa i percorsi restituiti con read_file per leggere i file completi.
"""
trovati = []

# FIX: il break precedente interrompeva solo il ciclo interno (for nome).
# Ora il controllo su max_results viene applicato anche al ciclo esterno
# (for root), in modo da fermare davvero la scansione al raggiungimento
# del limite.
for root, dirs, files in os.walk(BASE_DIR):
    if len(trovati) >= max_results:
        break
    dirs.sort()
    for nome in sorted(files):
        if len(trovati) >= max_results:
            break
        fp  = os.path.join(root, nome)
        ext = Path(fp).suffix.lower()
        if ext not in ESTENSIONI_LEGGIBILI:
            continue

        # Salta file troppo grandi per evitare scansioni eccessive
        try:
            if os.path.getsize(fp) > MAX_SEARCH_FILE_BYTES:
                log.info(
                    "search_content: saltato %s (supera %d byte)",
                    fp, MAX_SEARCH_FILE_BYTES,
                )
                continue
        except OSError:
            continue

        testo = _estrai_testo(fp, max_chars=500_000)
        if keyword.lower() in testo.lower():
            relativo = os.path.relpath(fp, BASE_DIR)
            idx      = testo.lower().find(keyword.lower())
            inizio   = max(0, idx - 80)
            fine     = min(len(testo), idx + len(keyword) + 80)
            estratto = testo[inizio:fine].replace("\n", " ").strip()
            if inizio > 0:
                estratto = "..." + estratto
            if fine < len(testo):
                estratto = estratto + "..."
            trovati.append(f"{relativo}\n  contesto: {estratto}")

log.info("search_content: keyword=%r -> %d risultati", keyword, len(trovati))

if not trovati:
    return (
        f"Nessun file contiene '{keyword}'. "
        "Prova con una parola chiave più generica o verifica l'ortografia."
    )

intestazione = f"File che contengono '{keyword}' ({len(trovati)} trovati"
if len(trovati) == max_results:
    intestazione += f", mostrati i primi {max_results}"
intestazione += "):\n\n"

return intestazione + "\n\n".join(trovati)
```

# —————————————————————————

# AVVIO

# —————————————————————————

if **name** == “**main**”:
import uvicorn

```
# Inizializza il database e ricarica i token attivi in memoria
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
    Route("/.well-known/oauth-authorization-server", well_known,      methods=["GET"]),
    Route("/.well-known/oauth-protected-resource",   well_known,      methods=["GET"]),
    Route("/authorize",                              authorize,        methods=["GET"]),
    Route("/token",                                  token_endpoint,   methods=["POST"]),
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