# Cos'è un firewall

Quando un server è connesso a internet è raggiungibile da chiunque nel mondo.

Il firewall è il programma che fa da filtro decidendo quale traffico può entrare nel server e quale deve essere bloccato.

## Come funziona

Il firewall ha un insieme di regole che specificano quali connessioni sono permesse.
Le regole più comuni si basano sulla *[porta](porta-di-rete.md)* a cui è indirizzata la richiesta.

## Perché è rilevante in questa guida

In questa guida il firewall deve permettere il traffico su due porte specifiche:

- **Porta 80**: necessaria a *[certbot](certbot.md)* per ottenere e rinnovare il *[certificato SSL](certificato-ssl.md)*
- **Porta 443**: necessaria a Claude.ai per connettersi al *[server MCP](mcp.md)*

Se una di queste porte è bloccata, il server non funziona correttamente anche se il programma Python è avviato e tutto il resto è configurato bene.