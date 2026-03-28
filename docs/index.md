---
title: Guida creazione server MCP per documenti aziendali
---
## Guida creazione server MCP

Un _[server MCP](wiki/mcp.md)_ (Model Context Protocol) permette a Claude di elencare tutti i file e le cartelle condivise in azienda e leggerne il contenuto (testo semplice, PDF, DOCX, XLSX)

> [!info] In questa guida sono stati inseriti dei link, riconoscibili dal corsivo (es. _[server MCP](wiki/mcp.md)_), che spiegano meglio alcune sezioni per renderla più comprensibile.

---

## Requisiti

- Python > 3.10
- Dominio che punta all'_[IP pubblico](wiki/indirizzo-ip.md)_ del server (`mcp.a4x.it`)
- _[Porta 80](wiki/porta-di-rete.md)_ raggiungibile per il rilascio del _[certificato](wiki/certificato-ssl.md)_
- Porta 443 raggiungibile per il server.
- A _[questa pagina](wiki/script.md)_ è possibile recuperare il codice python del server MCP.

---

## Configurazione iniziale

### Aggiornare python

```bash
apt update && apt install python3 python3-pip python3-venv certbot acl -y
```

### Creare una cartella per il progetto

```
mkdir -p /opt/mcp-fileserver
cd /opt/mcp-fileserver
```

### Creare un _[ambiente virtuale python](wiki/ambiente-virtuale-python.md)_

```
python3 -m venv venv
source venv/bin/activate
```

### Installare le librerie

```
pip install "mcp>=1.6,<2" uvicorn pymupdf python-docx openpyxl watchdog xlrd
```

### Salvare il codice del server

Creare il file che conterrà il codice Python del server:

```bash
nano /opt/mcp-fileserver/server.py
```

Incollare il contenuto disponibile a _[questa pagina](wiki/script.md)_, salvare e chiudere (`Ctrl+X`, poi `Y`, poi `Invio`).

---

## Ottenere il certificato SSL

_[Certbot](wiki/certbot.md)_ si occupa di verificare il certificato con _[Let's Encrypt](wiki/lets-encrypt.md)_.

```bash
certbot certonly --standalone -d mcp.a4x.it
```

Al termine il certificato si trova in:

```
/etc/letsencrypt/live/mcp.a4x.it/fullchain.pem   # certificato
/etc/letsencrypt/live/mcp.a4x.it/privkey.pem      # chiave privata
```

_[Cos'è una chiave privata](wiki/chiave-privata.md)_

### Rinnovo automatico

Let's Encrypt rilascia certificati validi 90 giorni. Certbot in automatico installa un timer che li rinnova prima della scadenza. Per verificare che sia attivo:

```bash
systemctl status certbot.timer
```

Poiché il rinnovo usa la porta 80 assicurarsi che sia sempre raggiungibile.

I certificati in `/etc/letsencrypt/live/` sono leggibili solo da utente root. È necessario dare accesso di lettura a un utente dedicato `mcp-user`:

```bash
# Creare l'utente di sistema dedicato:
useradd --system --no-create-home mcp-user
# dare i permessi di lettura a mcp-user
chgrp -R mcp-user /etc/letsencrypt/live/ /etc/letsencrypt/archive/
chmod -R g+rX /etc/letsencrypt/live/ /etc/letsencrypt/archive/
```

---

## File dei segreti

I valori sensibili per accedere al server MCP non vanno nel codice python. È necessario creare un file che li contenga e che il codice legga:

```bash
mkdir -p /etc/mcp-fileserver
nano /etc/mcp-fileserver/secrets
```

Per generare `MCP_CLIENT_SECRET`:

```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
```

Contenuto del file secrets (sostituire i valori della cartella da condividere e il codice segreto):

```
MCP_BASE_DIR=/percorso/cartella/condivisa
MCP_CLIENT_ID=mcp-aziendale
MCP_CLIENT_SECRET=incolla il testo generato in precedenza
MCP_ALLOWED_HOST=mcp.a4x.it
MCP_SSL_CERTFILE=/etc/letsencrypt/live/mcp.a4x.it/fullchain.pem
MCP_SSL_KEYFILE=/etc/letsencrypt/live/mcp.a4x.it/privkey.pem
MCP_DB_PATH=/opt/mcp-fileserver/mcp.db
```

Proteggere il file:

```bash
chmod 640 /etc/mcp-fileserver/secrets
chown root:mcp-user /etc/mcp-fileserver/secrets
```

---

## Configurazione come _[servizio systemd](wiki/systemd.md)_

**Dare i permessi corretti:**

```bash
# Permessi sulla cartella del progetto
chown -R mcp-user:mcp-user /opt/mcp-fileserver

# Accesso in sola lettura alla cartella documenti
setfacl -R -m u:mcp-user:rX /percorso/cartella/condivisa

# Accesso ai certificati (esegui dopo aver creato mcp-user)
chgrp -R mcp-user /etc/letsencrypt/live/ /etc/letsencrypt/archive/
chmod -R g+rX /etc/letsencrypt/live/ /etc/letsencrypt/archive/
```

**Creare il file di servizio systemd:**

```bash
nano /etc/systemd/system/mcp-fileserver.service
```

Contenuto del file:

```ini
[Unit]
Description=MCP File Server Aziendale
After=network.target

[Service]
ExecStart=/opt/mcp-fileserver/venv/bin/python /opt/mcp-fileserver/server.py
Restart=always
User=mcp-user
WorkingDirectory=/opt/mcp-fileserver
Environment=PYTHONUNBUFFERED=1
EnvironmentFile=/etc/mcp-fileserver/secrets
AmbientCapabilities=CAP_NET_BIND_SERVICE
CapabilityBoundingSet=CAP_NET_BIND_SERVICE

[Install]
WantedBy=multi-user.target
```

> [!info] Le righe `AmbientCapabilities` e `CapabilityBoundingSet` permettono a `mcp-user`, che non è root, di aprire la porta 443. Senza di esse il servizio si avvierebbe ma fallirebbe immediatamente al tentativo di mettersi in ascolto.

**Avvio del servizio:**

```bash
systemctl daemon-reload
systemctl enable mcp-fileserver
systemctl start mcp-fileserver
```

**Verificare che sia attivo:**

```bash
systemctl status mcp-fileserver
```

**Per vedere i _[log](wiki/log.md)_ in tempo reale:**

```bash
journalctl -u mcp-fileserver -f
```

### Rinnovo del certificato e riavvio del servizio

Quando certbot rinnova il certificato, il server MCP deve essere riavviato per caricare i nuovi file. Questo file permette il riavvio automatico e viene fatto partire da Certbot. Certbot ha un timer visto in precedenza che controlla due volte al giorno se i certificati sono prossimi alla scadenza. Quando decide che è il momento di rinnovare, esegue automaticamente tutti gli script che trova nella cartella `/etc/letsencrypt/renewal-hooks/deploy/`:

```bash
nano /etc/letsencrypt/renewal-hooks/deploy/restart-mcp.sh
```

Contenuto:

```bash
#!/bin/bash
systemctl restart mcp-fileserver
```

Renderlo eseguibile:

```bash
chmod +x /etc/letsencrypt/renewal-hooks/deploy/restart-mcp.sh
```

---

## Configurazione in Claude Team

### Per l'amministratore

1. Vai su [claude.ai](https://claude.ai/)
2. Apri **Organization settings** (in alto a destra, menu account)
3. Vai nella sezione **Connectors**
4. Clicca **Add custom connector**
5. Compila i campi:
    - **Name:** File Server Aziendale
    - **URL:** `https://mcp.a4x.it`
    - Clicca su **Advanced settings**
    - *[OAuth Client ID](wiki/oauth-client-id-secret.md): `mcp-aziendale`
    - _[OAuth Client Secret](wiki/oauth-client-id-secret.md):_ il valore di `MCP_CLIENT_SECRET` nel file dei segreti
6. Clicca **Add**

### Per i dipendenti

Ogni membro del team deve andare su **Settings > Connectors**, trovare il connector con etichetta "Custom" e cliccare **Connect** per autenticarsi la prima volta.