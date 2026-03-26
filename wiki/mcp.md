# Cos'è un server MCP

Di base Claude conosce solo quello che c'è nel messaggio: non può aprire da solo i file del computer/server e non ha accesso ai documenti della tua azienda.

Un server MCP colma questo limite.

## Come funziona

MCP (Model Context Protocol) è un sistema che permette a Claude di interagire con strumenti esterni.

Quando si pone una domanda a Claude del tipo "trova il contratto con il fornitore X", Claude usa questi strumenti in autonomia: cerca innanzitutto file con "contratto" nel nome, legge quelli pertinenti e risponde.

## Cosa cambia rispetto ad allegare un file

Allegare i file manualmente incontra dei limiti:

- Bisogna sapere quale file aprire e dove trovarlo
- Se i documenti sono tanti o aggiornati spesso diventa scomodo
- Non tutti sanno dove trovare i file giusti

Con un server MCP, Claude ha accesso in lettura alla cartella condivisa e può orientarsi da solo.