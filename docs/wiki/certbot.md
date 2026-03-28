# Cos'è certbot e a cosa serve

Per far funzionare il *[server MCP](mcp.md)* è necessario un *[certificato SSL](certificato-ssl.md)*. Il certificato lo rilascia un'autorità esterna chiamata *[Let's Encrypt](lets-encrypt.md).* 
Ottenerlo e tenerlo aggiornato richiede una serie di passaggi tecnici che andrebbero ripetuti ogni 90 giorni.

Certbot automatizza tutto questo.

## Cosa fa certbot

La prima volta, certbot esegue tre operazioni:

1. Contatta Let's Encrypt e chiede un certificato per il dominio
2. Dimostra di esser il proprietario del dominio avviando temporaneamente un piccolo server sulla *[porta](porta-di-rete.md)* 80, che Let's Encrypt contatta per verificare
3. Scarica il certificato e lo salva sul server in una cartella dedicata

Da quel momento in poi certbot si occupa di rinnovare il certificato prima che scada.

## Perché la porta 80 deve essere libera

Durante la verifica, Let's Encrypt contatta il tuo server all'indirizzo `http://mcp.a4x.it`. Se la porta 80 è bloccata dal *[firewall](firewall.md)* o occupata da un altro programma la verifica fallisce e il certificato non viene rilasciato.

Questa porta serve solo durante il rilascio e il rinnovo del certificato, non durante il normale funzionamento del *[server MCP](mcp.md)*.

## Dove finisce il certificato

Certbot salva i file del certificato in:

```
/etc/letsencrypt/live/mcp.a4x.it/
```

Questa cartella contiene due file che il server MCP usa per cifrare le comunicazioni: il certificato vero e proprio e la *[chiave](chiave-privata.md)* privata associata. La chiave privata non va mai condivisa o resa pubblica.
