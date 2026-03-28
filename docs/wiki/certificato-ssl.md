# Cos'è un certificato SSL e a cosa serve

Quando si visita un sito web, il browser e il server devono scambiarsi informazioni: password, dati ecc.
Queste informazioni viaggiano attraverso internet passando per decine di computer intermedi prima di arrivare a destinazione. Chiunque si trovasse in mezzo potrebbe leggerle o modificarle.

Il certificato SSL serve a impedire che questo accada.

## Come funziona

Immaginiamo di voler inviare una lettera segreta a un amico. Prima di farlo ci si mette d'accordo su un codice segreto per decifrare la lettera. Da quel momento anche se qualcuno intercettasse la lettera vedrebbe solo simboli incomprensibili.

Il certificato SSL permette al browser e al server di mettersi d'accordo su un codice segreto in modo che tutta la comunicazione sia cifrata. Nessun intermediario può leggerla.

È possibile riconoscere una connessione protetta dal lucchetto che appare nella barra degli indirizzi del browser e dal fatto che l'indirizzo inizia con `https://` invece di `http://`.

## Chi rilascia i certificati

Un certificato deve essere rilasciato da un'autorità di certificazione, un'organizzazione di cui browser e sistemi operativi si fidano per garantire che il certificato sia autentico.

In questo progetto è usato *[Let's Encrypt](lets-encrypt.md)*, un'autorità di certificazione gratuita e automatica. Prima di rilasciare il certificato per il dominio Let's Encrypt verifica chi ne sia effettivamente il proprietario: lo fa contattando il server sulla *[porta](porta-di-rete.md)* 80 e controllando che risponda in modo corretto. 

## Perché la porta 443

Una volta ottenuto il certificato, il tuo server inizia a comunicare in modo cifrato. Per convenzione, le connessioni HTTPS usano la **porta 443**.

Se la porta 443 non è raggiungibile dall'esterno, Claude non riesce a connettersi al server.
## I certificati scadono

I certificati SSL hanno una durata limitata e i certificati rilasciati da Let's Encrypt durano 90 giorni.
Non è un problema dato che il rinnovo avviene in automatico.
