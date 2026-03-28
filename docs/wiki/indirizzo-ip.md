# Cos'è un indirizzo IP

Ogni dispositivo connesso a internet ha un indirizzo che lo identifica in modo univoco come il numero civico di una casa. Questo indirizzo si chiama indirizzo IP ed è composto da quattro numeri separati da punti, ad esempio `93.184.216.34`.

Quando il browser vuole caricare una pagina il computer usa quell'indirizzo per sapere a quale server mandare la richiesta.

## IP pubblico e IP privato

L'**IP privato** è l'indirizzo che un dispositivo ha all'interno di una rete locale, ad esempio quella di casa o dell'ufficio. È visibile solo ai dispositivi nella stessa rete e non è raggiungibile da internet. Gli indirizzi privati sono solitamente nella forma `192.168.x.x` o `10.x.x.x`.

L'**IP pubblico** è l'indirizzo che identifica un dispositivo su internet, visibile e raggiungibile da qualsiasi altro computer nel mondo. È assegnato dal provider internet.

## Perché in questa guida serve un IP pubblico

Il *[server MCP](mcp-md)* deve essere raggiungibile da Claude. 

## Il dominio come alternativa all'IP

Ricordare un indirizzo come `93.184.216.34` è scomodo. Per questo esiste il sistema dei domini: invece di digitare l'IP, digiti un nome come `mcp.a4x.it` e il sistema lo traduce nell'IP corrispondente.