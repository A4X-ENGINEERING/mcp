# Cos'è Let's Encrypt

Un [certificato SSL](certificato-ssl.md) valido deve essere firmato da un'organizzazione di cui browser e sistemi operativi si fidano. Queste organizzazioni si chiamano autorità di certificazione. Una di queste è gratuita: Let's Encrypt.

## Perché esiste

Prima di Let's Encrypt ottenere un certificato SSL costava decine o centinaia di euro all'anno e richiedeva procedure manuali e macchinose. Questo faceva sì che molti siti, soprattutto piccoli, girassero senza nessuna cifratura mettendo a rischio i dati degli utenti.

Let's Encrypt è nata nel 2016 con l'obiettivo di rendere HTTPS accessibile a tutti. È gestita da una fondazione no-profit chiamata Internet Security Research Group ed è finanziata da grandi aziende come Google, Mozilla e Cisco.

## Perché i certificati durano solo 90 giorni

Let's Encrypt rilascia certificati con una durata massima di 90 giorni, molto più breve rispetto ad altre autorità che arrivano a uno o due anni. La scelta è intenzionale: se un certificato venisse rubato o compromesso smetterebbe di funzionare entro pochi mesi senza bisogno di intervento manuale.