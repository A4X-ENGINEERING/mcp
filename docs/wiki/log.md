# Cos'è un log

Quando un programma gira in background non mostra messaggi sullo schermo. Per sapere cosa sta facendo, se ha incontrato errori o quante richieste ha ricevuto, i programmi scrivono continuamente un diario definito log.

## Come funziona

Ogni volta che succede qualcosa di rilevante, il programma aggiunge una riga al log con l'orario esatto e una descrizione dell'evento. Ad esempio:

```
2025-03-21 10:42:01  Richiesta ricevuta: list_all
2025-03-21 10:42:01  Risposta inviata: 24 file trovati
2025-03-21 10:43:17  Errore: file non trovato - contratto_2023.pdf
```

Leggendo il log puoi ricostruire tutto quello che è successo.

## A cosa serve

Il log è lo strumento principale per capire cosa non funziona. Se il *[server MCP](mcp.md)* non risponde, se Claude non riesce a leggere un file, se qualcosa si comporta in modo strano, la prima cosa da fare è controllare il log: quasi sempre contiene la spiegazione del problema.

## Come si consultano i log

Su Linux i log dei servizi *[systemd](systemd.md)* si consultano con il comando `journalctl`:

```bash
journalctl -u mcp-fileserver -f
```

Il parametro `-u` indica il nome del servizio, mentre `-f` fa sì che il terminale rimanga aperto e mostri le nuove righe man mano che vengono scritte. Per uscire basta premere `Ctrl+C`.