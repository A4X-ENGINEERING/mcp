# Cos'è OAuth e a cosa servono Client ID e Client Secret

Quando Claude vuole connettersi al *[server MCP](mcp.md)*, il server deve poter verificare che la richiesta venga davvero da Claude e non da qualcun altro. Questo meccanismo di verifica si chiama autenticazione.

OAuth è uno standard che definisce come due programmi si autenticano tra loro senza che l'utente debba inserire una password ogni volta.

## Client ID e Client Secret

Il **Client ID** è il nome identificativo del programma. È pubblico: serve solo a dire "sono io, il connettore MCP aziendale". In questa guida il valore è `mcp-aziendale`.

Il **Client Secret** è la password associata a quel nome. È privato e non va condiviso o scritto in chiaro nel codice. In questa guida viene generato casualmente e salvato nel file dei segreti. Tenendolo in un file separato con permessi ristretti, solo i programmi autorizzati possono leggerlo, senza rischiare che finisca in backup del codice.
