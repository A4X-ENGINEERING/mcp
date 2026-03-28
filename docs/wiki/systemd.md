# Cos'è un servizio systemd

Quando si installa un programma su Linux e si vuole che giri continuamente in background, che parta in automatico all'avvio del server e che si riavvii da solo, è opportuno trasformarlo in un **servizio**.

Systemd è il programma che gestisce tutti i servizi su Linux. È il primo programma che parte quando il server si avvia e si occupa di avviare, fermare e monitorare tutto il resto.

## Cos'è un servizio

Un servizio è un programma che gira in background senza che nessuno lo stia usando attivamente.

## Cosa si guadagna usando systemd

Senza systemd si dovrebbe avviare il *[server MCP](mcp.md)* a mano ogni volta che il server si riavvia, e non si avrebbe alcuna garanzia che rimanga attivo nel caso vada in errore. Con un file di servizio systemd:

- Il server MCP parte automaticamente all'avvio della macchina
- Se il processo va in crash, systemd lo riavvia in automatico
- I [log](log.md) vengono raccolti e sono consultabili con un comando

## Il file di servizio

Per dire a systemd come gestire un programma bisogna creare un file di testo con estensione `.service` nella cartella `/etc/systemd/system/`. Questo file descrive quale programma eseguire, con quale utente, in quale cartella, e cosa fare in caso di errore.

Una volta creato il file, lo si attiva con `systemctl enable` (per farlo partire all'avvio) e `systemctl start` (per avviarlo subito).