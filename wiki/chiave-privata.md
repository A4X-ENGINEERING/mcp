# Cos'è una chiave privata

Quando il server comunica con Claude.ai con *[connessione SSL](certificato-ssl.md)* usa un sistema basato su due chiavi: una pubblica e una privata.

## Come funziona la coppia di chiavi

La **chiave pubblica** è contenuta nel certificato SSL e può essere distribuita liberamente a chiunque. Chiunque può usarla per cifrare un messaggio destinato al server.

La **chiave privata** è l'unica in grado di decifrare quei messaggi. Deve rimanere sul server e non va condivisa.
## Dove si trova in questa guida

La chiave privata generata da *[certbot](certbot.md)* si trova in:

```
/etc/letsencrypt/live/mcp.a4x.it/privkey.pem
```

Il file `.pem` è un file di testo che contiene la chiave. Non va copiato o caricato su servizi esterni.
