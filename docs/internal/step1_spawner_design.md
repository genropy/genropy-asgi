# Step 1 — Spawner + sticky-per-user: ragionamento e ciclo di vita

**Version**: 0.1.0
**Status**: 🔴 DA REVISIONARE
**Last Updated**: 2026-06-24

> Documento di DESIGN (proposta, non verità). Consolida il ragionamento logico
> sul ciclo di vita dello Step 1, condotto con l'utente. I FATTI verificati sul
> ruolo del daemon e sulla direzione tecnica stanno in
> [legacy_daemon_role.md](legacy_daemon_role.md) — questo documento li cita e
> NON li ripete. Metodo seguito (richiesto dall'utente): prima il ragionamento
> logico e il ciclo di vita, POI l'implementazione; il codice esistente non è
> guida, al massimo confronto a posteriori (coincide → si tiene; no → si rifà).

---

## Obiettivo dello Step 1

Mettere a punto **spawner + sticky-per-utente**, sopra il daemon ancora attivo,
**HTTP-only** (WebSocket rimandato). Niente rimozione del daemon (è lo Step 2).
Con il daemon attivo lo sticky è un'**ottimizzazione**, non un requisito di
correttezza: un instradamento sbagliato non rompe (lo stato condiviso copre), e
questo permette di rendere solido lo spawner senza risolvere insieme
l'identità/migrazione di stato.

## Architettura: un cervello + N legacy-worker minimali

Distinzione fissata con l'utente:

- **Server completo (genro-asgi)** — UNO. Multi-app, configurazione, middleware,
  plugin, i registri (`RequestRegistry`/`WsxRegistry`), l'orchestrazione,
  l'accoglienza. È il cervello: decide, instrada, ospita i guest. Vede il browser.
- **legacy-worker** — N. Il minimo per eseguire UN sito GenroPy legacy via
  uvicorn: `richiesta HTTP → smartasync(_run_wsgi) → thread → GnrWsgiSite →
  risposta`. NIENTE multi-app, middleware, plugin, registri propri,
  orchestrazione. Non vede mai il browser: riceve solo richieste inoltrate dal
  cervello. È un esecutore spartano, sacrificabile.

Il ponte ASGI→WSGI dell'esecutore esiste già nella sostanza di `GenropyProxy`
(`genropy_proxy.py:81-102`, `_run_wsgi` `:213`). DA VERIFICARE: quanto
`GenropyProxy` è separabile da `AsgiServer` (usa `self.server.request_registry`
in `handle_request`) — definisce se il legacy-worker è un `GenropyProxy`
sfrondato o un esecutore ancora più nudo (uvicorn → adapter WSGI → GnrWsgiSite).

## Ciclo di vita (ragionato con l'utente)

Due regimi dello stesso sistema, transizione graduale guidata dal carico:

1. **Avvio**: 1 processo, il cervello. 0 utenti, 0 worker.
2. **Cliente piccolo (pochi utenti)**: il cervello fa **anche** da operatore —
   serve accoglienza E utenti loggati, sotto il suo GIL. È il sistema attuale.
   Zero overhead, non spawna mai worker. Caso reale (clienti da 2-3 utenti).
3. **Cresce**: superata una soglia di carico, il cervello comincia a spawnare
   legacy-worker e a mandarci i NUOVI utenti loggati; tende a "ritirarsi" verso
   il ruolo di solo-accoglienza + orchestrazione (l'orchestrazione è I/O-bound,
   convive col GIL; gli utenti pesanti — CPU/ORM/register — stanno sui worker).
4. **Capienza e early-spawn** (numeri d'esempio dell'utente): il primario ospita
   fino a 6 utenti; **a 4** avvia un nuovo worker (così è pronto prima di
   saturare — il boot di un GnrWsgiSite costa ~secondi). Capienza esaurita → un
   altro worker. Crescita **elastica, su domanda**.

Elastico verso l'alto, gratuito verso il basso: diventa distribuito solo se e
quando serve.

## Lo stato dello spawner

Dall'analisi (workflow adversariale): **due strutture, non tre**.

- **children** — `alloc_id → Allocation(host, port, status, handle)`. La flotta
  dei processi worker. Topologia, nessuna identità.
- **registro per-utente** (il "dizionario utenti ricco" dell'utente) — per ogni
  utente: `worker_attuale`, `worker_target` (se ≠ attuale → migrazione
  pendente), e **`inflight`**.

**Niente dict connessioni nello spawner** (verdetto confermato): le connessioni
vivono nel worker. La fase guest non richiede un dict connessioni, richiede solo
di chiavizzare per connection_id (forma `cid:` della chiave). Un dict connessioni
nello spawner sarebbe dannoso: via HTTP il proxy vede la richiesta successiva, mai
la chiusura del socket → non potrebbe mai fare GC delle entry → seconda fonte di
verità in drift col registro del worker.

### inflight: le request, NON un contatore

Scelta dell'utente, motivata: `inflight` NON è un intero +1/−1 ma un **dict di
request per id**: `inflight: { request_id: (path, started_at, ...) }`.

Perché non il contatore secco ("se leggo 2 e non so che request sono?"):
- una request appesa che non torna mai → col contatore "in volo: 2" per sempre,
  non migri mai e non sai QUALE è bloccata; col dict vedi la request, da quanto,
  su che path → puoi gestire timeout/recovery;
- niente disallineamento (un finally saltato non manda il contatore a −1);
- osservabilità: "questo utente è fermo da 30s, cosa fa?".
- `len(inflight)` dà comunque il conteggio quando basta.

Chiave = il `request.id` che genri-asgi già assegna a ogni request
(`request.py:811`). Stesso aggancio del +1/−1 (popola all'inoltro, rimuovi alla
risposta), più informazione.

## Quiescenza e migrazione opportunistica

- **Quiescente** = `inflight` vuoto per quell'utente.
- La migrazione **non è in blocco**: lo spawner marca `worker_target`; le NUOVE
  richieste dell'utente continuano ad andare al `worker_attuale` (coerenza: niente
  richieste sparse tra vecchio e nuovo); appena l'utente è quiescente
  (`inflight` vuoto) → `worker_attuale ← worker_target`, azzera target. Per-utente,
  lazy, al confine naturale tra le sue richieste.
- È il **connection draining** dei load balancer, per-utente. Con HTTP breve la
  quiescenza arriva in millisecondi → migrazione quasi istantanea.
- Caso limite segnalato: un utente che non diventa mai quiescente non migrerebbe
  mai. Improbabile con richieste brevi + think-time umano; non-problema pratico.
- Step 1, "i suoi già presenti restano o si spostano?" — i nuovi vanno ai worker;
  i già-presenti sul primario possono RESTARE (più semplice, niente trasferimento)
  oppure essere drenati. Per il piccolo è irrilevante (non supera mai soglia).

## I registri di genro-asgi (verificato) e cosa manca

- `RequestRegistry` (`request.py:762-856`): traccia ogni request in volo per
  `request.id`; ingresso/uscita netti `create`/`unregister` nel try/finally di
  `handle_request` (`asgi_application.py:240-270`). Aggrega **per app**
  (`count_by_app`, `:831`) e globale (`__len__`) — **NON per utente**.
- `WsxRegistry` (`wsx/registry.py:72-158`): traccia le connessioni WS per
  `connection_id`, con `find_by_identity(user)` (per-utente, c'è già).
- **Manca l'aggregazione per-utente delle request HTTP.** Il registro per-utente
  dello spawner è quindi una struttura NUOVA, di natura diversa: i registri di
  genro-asgi tracciano "cosa eseguo io" (per-app, multi-app); il registro
  per-utente traccia "chi ho mandato dove e cosa ha in volo" (orchestrazione). Non
  è una mancanza di genro-asgi: è un altro livello di aggregazione.

## WSX = pseudo-request per messaggio (fatto verificato, rilevante per il futuro)

`WsxHandler._dispatch_message` (`wsx/handler.py:183-202`) crea **una pseudo-request
nel RequestRegistry per OGNI messaggio** WSX (non una request lunga per la
connessione), e passa per lo stesso flusso delle HTTP (`node` + `smartasync`,
`:205-218`). Conseguenza: la quiescenza si misura sulle **pseudo-request**, quindi
varrà anche per le WS — un utente è quiescente *tra un messaggio e l'altro*. La
connessione WS persistente vive nel `WsxRegistry`; il lavoro sono pseudo-request
brevi con ingresso/uscita netto. NB: per la migrazione WS resterà comunque il
problema del *canale* WS fisico aperto (da affrontare oltre lo Step 1 HTTP-only).

## Routing (regola per richiesta)

Decodifica cookie una volta (HMAC, zero I/O, nessun giro sul worker). Risolvi il
canale (xgroup minuscolo, fallback `standard`).
- **Guest** (cookie `user=guest_*` o assente): il cervello lo serve in proprio
  (accoglienza), oppure va al welcome; chiave `cid:<connection_id>` (fallback
  `tcp:`).
- **Loggato**: chiave `usr:<username>`; instrada al `worker_attuale` dal registro
  per-utente. Al primo login, carry-over: se l'utente non ha ancora un worker, gli
  si assegna (capienza/least-loaded). Tutte le sue connessioni convergono lì.

Il login avviene SUL cervello (serve lui l'accoglienza); dalla richiesta
successiva (utente ormai loggato) instrada al worker. Col daemon (Step 1) è
indolore: identità/connessione create durante il login sono nel daemon, il worker
le ritrova. (Nello Step 2 questo è IL punto da risolvere.)

## Rischi noti (dalla critica adversariale)

1. **Nome cookie in multidominio** — il decode cerca il morsello sotto il nome
   `site`; GenroPy lo scrive sotto `currentDomainIdentifier` (= `site` solo in
   single-domain; `site|domain` altrove). Fuori single-domain il decode fallisce
   in silenzio → tutto collassa sul fallback `tcp:` → sticky/bilanciamento
   illusori. Step 1: la correttezza regge (il daemon possiede lo stato), ma lo
   sticky fuori single-domain no. Allineare il nome cookie a
   `currentDomainIdentifier` prima di affidarsi allo sticky.
2. **Boot worker ~secondi** — l'utente che fa nascere il worker aspetta;
   mitigabile pre-spawnando il prossimo worker prima che serva (early-spawn a 4).

## Aperto / da decidere

- legacy-worker minimale: `GenropyProxy` sfrondato vs esecutore nuovo nudo (DA
  VERIFICARE la dipendenza GenropyProxy↔AsgiServer).
- Capienza: confermata = numero di **utenti** per worker.
- Già-presenti sul primario al superamento soglia: restano o si drenano.
- Pre-warm del prossimo worker (sì/no) per azzerare l'attesa al login.

## Non vincolante (codice esplorativo, NON fonte di verità)

Tutto ciò che diverge da genropy `develop` e il codice sticky del branch
`feature/poc-sticky-workers` (`GenropyStickyProxy`, `sticky_*`) è esplorativo,
sacrificabile, e se tenuto NON è verità. Lo sticky va RIFATTO da zero con questo
modello — il codice esistente è al più uno specchio che ha validato la fattibilità
(i file:riga confermano che il modello è realizzabile), non la base.
