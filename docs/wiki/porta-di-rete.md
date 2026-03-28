# Cos'è una porta di rete

Quando due computer comunicano su internet non basta sapere l'[indirizzo IP](indirizzo-ip.md) del computer di destinazione, bisogna anche specificare a quale programma è destinato il messaggio. Su un server possono girare decine di programmi contemporaneamente, ognuno in ascolto su un canale diverso.

Questi canali si chiamano porte.

## Come funziona

Pensa a un grande palazzo con tanti appartamenti. L'indirizzo del palazzo è unico, ma ogni appartamento ha un numero diverso. Quando si vuole visitare qualcuno bisogna sapere il numero dell'appartamento oltre all'indirizzo di casa.

Su un server l'indirizzo del palazzo è l'indirizzo IP del server e i numeri degli appartamenti sono le porte. Ogni programma risponde solo ai messaggi indirizzati a una specifica porta.

## Le porte più comuni

Alcune porte hanno uno standard internazionale che i programmi rispettano per convenzione:

- **Porta 80**: usata per le connessioni HTTP, cioè i siti web non cifrati
- **Porta 443**: usata per le connessioni HTTPS, cioè i siti web cifrati con SSL

In questa guida il *[server MCP](mcp.md)* usa la porta 443 per rispondere a Claude, e la porta 80 viene usata temporaneamente da *[certbot](certbot.md)* per ottenere il *[certificato](certificato-ssl.md)*.

## Cosa significa che una porta è raggiungibile

Una porta è raggiungibile quando il *[firewall](firewall.md)* del server permette al traffico esterno di arrivarci. Se una porta è bloccata, i messaggi arrivano al server ma vengono scartati prima ancora di raggiungere il programma. È come se il portone del palazzo fosse sbarrato: puoi suonare il campanello ma nessuno ti aprirà mai.
