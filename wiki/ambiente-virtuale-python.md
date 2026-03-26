# Cos'è un ambiente virtuale Python

Python come tutti i linguaggi di programmazione ha un ecosistema di librerie: codici scritti da altri che puoi usare nei tuoi programmi senza doverli scrivere da zero. In questo progetto usiamo `uvicorn` e `pypdf`.

Il problema sorge nel momento in cui librerie diverse entrano in conflitto tra loro. 
Se sul server girano più programmi Python ognuno potrebbe aver bisogno di versioni diverse delle stesse librerie.

Un ambiente virtuale risolve questo problema creando una cartella isolata che contiene una copia di Python e tutte le librerie necessarie solo per quel progetto.

## Come funziona

Quando esegui:

```bash
python3 -m venv venv
source venv/bin/activate
```

La prima riga crea la cartella `venv` con tutto l'occorrente. La seconda riga "attiva" l'ambiente: da quel momento in poi, qualsiasi libreria installi con `pip` non interferisce con il resto del sistema.

Quando l'ambiente è attivo, il terminale lo segnala mostrando il nome dell'ambiente all'inizio della riga:

```
(venv) root@server:~#
```

## Perché è importante in questa guida

Il file di servizio *[systemd](systemd.md)* avvia il *[server MCP](mcp.md)* usando l'interprete Python dentro la cartella `venv`:

```
/opt/mcp-fileserver/venv/bin/python
```

Questo garantisce che il server usi sempre le librerie giuste, indipendentemente da cosa sia installato nel resto del sistema.
