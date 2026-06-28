# Analisi performance — genro-asgi servendo GenroPy legacy

**Version**: 0.3.0
**Status**: 🔴 DA REVISIONARE
**Last Updated**: 2026-06-20

> Documento di lavoro interno (`docs/internal/`). Raccoglie i test eseguiti, i
> numeri misurati e i ragionamenti dell'indagine performance. Cresce nel tempo:
> ogni nuova sessione di test aggiunge una sezione datata. Lingua: italiano
> (coerente con `docs/analisi_asgi_delegation.md`, doc di analisi affine).

---

## Scopo

Capire come si comporta genro-asgi quando serve un sito GenroPy legacy (via il
proxy del contrib `genropy_asgi`), confrontarlo col WSGI legacy puro, individuare
i colli di bottiglia e decidere il modello di scaling futuro. Tema collegato:
[analisi_asgi_delegation.md](../analisi_asgi_delegation.md) (delegation ASGI) e
il modello gateway+worker (memoria di progetto).

---

## Sessione 2026-06-16

### Setup

- Istanza GenroPy: `test_invoice_pg` (Postgres su `:5432`).
- Pagina di test: `GET /` — la home page GenroPy reale (8172 byte). **Importante:
  non tocca il DB** (0 connessioni Postgres anche sotto carico): è rendering puro
  + round-trip al daemon. Quindi i numeri misurano CPU/rendering+daemon, non SQL.
- Strumento di carico: `hey` (installato via brew, `brew install hey`).
- Macchina: 10 core fisici/logici.
- Daemon: due implementazioni confrontate (vedi sotto).

### Test 1 — ASGI vs WSGI legacy puro (vecchio daemon Pyro4)

Prima passata (10 client, 200 req): risultati **inquinati** da un artefatto del
SO — esaurimento delle **porte effimere** (`can't assign requested address`).
Causa: il vecchio daemon (Pyro4) apre una connessione NUOVA per ogni chiamata; il
burst sature le porte locali → 500 sia sul legacy (10) sia sull'ASGI (20). Misura
non attendibile. Lezione: a concorrenza alta su macOS, l'apertura/chiusura rapida
di connessioni brucia le porte (TIME_WAIT lungo).

Seconda passata pulita (5 client, 100 req, keep-alive), **vecchio daemon**:

| | req/s | p50 | p99 | errori |
|---|---|---|---|---|
| WSGI dev (`gnrwsgiserve`) | 32,1 | 0,151 s | 0,212 s | 0 |
| ASGI (`gnrasgiserve`)     | 26,5 | 0,152 s | 0,515 s | 0 |

A basso carico **p50 quasi identico**: il layer ASGI+proxy non aggiunge overhead
percepibile sulla richiesta tipica. L'ASGI ha più varianza in coda (proxy
WSGI-dentro-ASGI).

### Test 2 — nuovo daemon (genro-daemon: asyncio + uvloop + msgpack)

Sostituito il vecchio daemon (Pyro4) col nuovo (repo `genropy/genro-daemon`,
drop-in, stessa porta `:40405`). Stessa pagina, stesso DB.

**Vecchio vs nuovo daemon — burst 10 client × 200 req** (lo stress che rompeva):

| | 200 OK | 500 | conn-err | latenza media | p99 |
|---|---|---|---|---|---|
| Vecchio (Pyro4)  | 170 | 10 | 20 | 0,300 s | 0,370 s |
| Nuovo (msgpack)  | **200** | **0** | **0** | **0,178 s** | **0,242 s** |

**Carico leggero 5 client** col nuovo daemon: p50 da 0,151 s → **0,075 s** (≈ metà).

**Conclusione**: il nuovo daemon elimina i 500/esaurimento porte sotto burst
(connessioni gestite, non una-per-chiamata) e dimezza la latenza. Miglioramento
reale e misurato.

### Test 3 — ASGI vs WSGI a parità di (nuovo) daemon

| Carico | Stack | req/s | p50 | p99 | errori |
|---|---|---|---|---|---|
| 5c×100  | ASGI | 65,4 | 0,075 | 0,110 | 0 |
|         | WSGI | 65,7 | 0,076 | 0,088 | 0 |
| 10c×200 | ASGI | 60,6 | 0,161 | 0,227 | 0 |
|         | WSGI | 60,7 | 0,162 | 0,207 | 0 |
| 25c×500 | ASGI | 60,1 | 0,425 | 0,499 | 0 |
|         | WSGI | 59,9 | 0,416 | 0,486 | 0 |

**ASGI e WSGI equivalenti** a tutti i livelli (differenze nel rumore di misura).
Il layer ASGI+proxy non penalizza rispetto al WSGI puro.

### Test 4 — ricerca del tetto (scale concorrenza)

WSGI dev e ASGI 1-processo, da 10 a 100 client: **throughput piatto a ~60 req/s**,
0 errori. Salendo la concorrenza cresce SOLO la latenza (p50 da 0,16 s @10c a
1,66 s @100c), non il throughput. Firma di una capacità seriale fissa a valle: i
client in più si accodano (legge di Little: 60 req/s × 1,66 s ≈ 100 in volo).

**Il limite è ~60 req/s per singolo processo.**

### Test 5 — gunicorn multi-worker (WSGI di produzione, già esistente)

GenroPy ha già un server gunicorn: `python -m gnr.web.cli.gnrserveprod <istanza>
-b <host:port> -w <N>` (`docs`: usa `gunicorn.app.base.BaseApplication`). Lanciato
con 4 worker:

| Concorrenza | dev 1-proc | gunicorn 4-worker |
|---|---|---|
| 10  | ~60 req/s | 166 req/s |
| 100 | ~60 req/s, p50 1,66 s | 166 req/s, p50 0,59 s |

Il tetto sale da ~60 a **~166 req/s** (≈ 2,8×) e la latenza crolla. Quindi il
limite a 60 NON era il DB né il daemon: era il **server single-process**.

### Test 6 — smartasync è il collo di bottiglia? (NO)

Ipotesi: `smartasync` (bridge sync→async) usa `asyncio.to_thread` (default thread
pool) → potrebbe serializzare l'ASGI. Verifica:
- thread del processo ASGI: **21 a riposo, 21 sotto 80 client** — il pool NON si
  espande. Se smartasync fosse il collo, i thread crescerebbero.
- ASGI 1-proc plafona a ~60 = identico a WSGI 1-proc (che non usa smartasync).
- Il tetto sale col numero di **processi**, non di thread.

**Smartasync NON è il collo.** Il limite di 1 processo è il **GIL** sul rendering
CPU-bound di GenroPy: un solo processo Python = un solo GIL = ~60 req/s, sync o
async che sia. Prova finale: ASGI 4-worker (gunicorn + `uvicorn.workers.
UvicornWorker` su `server_factory`) sale a ~120-160 req/s come gunicorn-WSGI →
4 processi = 4 GIL = scala.

Nota tecnica: `uvicorn --workers` NATIVO fallisce su `server_factory`
(`join() ... not 'coroutine'`, bug nello spawn). Workaround funzionante: gunicorn
+ UvicornWorker.

### Test 7 — modello dei processi (replicazione)

Sotto carico, con 4 worker:
- ogni worker = processo separato, ~40 MB RSS, con la SUA `GnrApp`/db_registry/
  cache modello **replicata** (non condivisa);
- ogni worker apre 7-10 connessioni al daemon;
- il daemon è **UNO solo** (`:40405`), punto di convergenza di tutti i worker.

Modello ibrido a 3 livelli: **worker** = processi isolati (scalano in parallelo,
costano N× memoria); **daemon** = stato condiviso del sito (siteregister:
sessioni/pagine/utenti), unico, punto di sincronizzazione. È ciò che permette a un
utente di essere servito da worker diversi mantenendo la sessione.

---

## Conclusioni della sessione

1. **60 req/s bastano oggi.** Il cliente più grosso (centinaia di utenti) picca a
   ~15 req/s → 4× di margine con UN solo processo ASGI. Nessun problema reale.
2. **Il nuovo daemon è un miglioramento netto** (niente 500 sotto burst, latenza
   dimezzata): da migrare in produzione quando opportuno.
3. **Il limite di un processo (~60 req/s) è il GIL** sul rendering, non smartasync
   né il DB. Per scalare serve multi-processo (gunicorn/UvicornWorker → ~166).
4. **Oltre, il muro è il daemon condiviso** (4 worker → 166, non 240).

## Direzione futura: dispatcher + esecutori sticky (NON ora)

Modello target (= memoria `project_worker_architecture.md`, confermato da questi
numeri): 1 dispatcher ASGI leggero che fa routing e DELEGA l'esecuzione a worker
GenroPy già caldi, con **affinità utente** (sticky: stesso utente → stesso worker).

Ragionamenti chiave (sviluppati in questa sessione):
- I worker "fratelli" (gunicorn) sprecano memoria (N copie del modello) e hanno
  load-balancing cieco. Meglio un dispatcher che instrada con criterio.
- La **conversione ASGI→WSGI è già pronta** in `GenropyProxy._build_environ` /
  `_run_wsgi` (contrib): il dispatcher sa già tradurre, basta redirigere a un
  processo invece che in-process.
- Punto di innesto unico: `asgi_application.py` `handle_request`, dove oggi fa
  `result = await smartasync(node)(...)`.
- **L'affinità utente esclude il ProcessPoolExecutor opaco** (`LocalExecutor`): il
  pool sceglie LUI il processo, non si può pinnare; e gli handler ORM non sono
  picklabili. Servono **worker indirizzabili** (processi su socket/porte proprie).
- **Insight: con worker sticky il daemon DECADE.** Lo stato (sessioni/connessioni)
  oggi sta nel daemon PERCHÉ i worker sono intercambiabili. Con l'affinità lo stato
  vive in-process nel worker dell'utente → il daemon non serve più come custode
  (al più resta coordinatore: assegnazione affinità + scoperta worker + backup).
  Sparisce così anche il collo di bottiglia ~166.

Nodi aperti del modello (da decidere quando lo si farà):
- **Trasporto** dispatcher→worker: conversione WSGI (HTTP/request) vs WebSocket
  diretto al worker (il modello gateway+worker parlava di WebSocket). Da unificare.
- **Resilienza**: se un worker muore, lo stato in-process dell'utente muore con lui
  (sessione persa). Il daemon dava durabilità/failover gratis. Trade-off
  località-dello-stato vs resilienza.
- **Granularità affinità**: per utente / sessione / connessione?
- **Sbilanciamento**: utente pesante pinnato → worker sovraccarico.

## NON riaprire (valutato e scartato)

- smartasync come collo di bottiglia → smentito dai numeri (è il GIL).
- esecutori via `LocalExecutor`/ProcessPoolExecutor opaco → incompatibile con
  l'affinità utente (no pinning) e con l'ORM (non picklabile).

## Comandi utili (riproducibilità)

```
# server di test (stessa istanza, daemon sotto)
gnrasgiserve test_invoice_pg -p 8090                                  # ASGI 1 proc
gnrwsgiserve test_invoice_pg -p 8091                                  # WSGI dev 1 proc
python -m gnr.web.cli.gnrserveprod test_invoice_pg -b 127.0.0.1:8092 -w 4   # WSGI gunicorn 4w
# ASGI multi-worker (NON uvicorn --workers nativo: fallisce):
GNR_ASGI_SITE=test_invoice_pg GENRO_ASGI_CONFIG=<.../genropy_config.py> \
  gunicorn "genro_asgi.server.server:server_factory()" \
  --worker-class uvicorn.workers.UvicornWorker --workers 4 --bind 127.0.0.1:8093

# carico
hey -n 200 -c 10 http://127.0.0.1:8090/      # burst
for c in 10 25 50 100; do hey -n $((c*20)) -c $c http://127.0.0.1:8090/; done   # scale

# monitoraggio
psql -d postgres -tAc "SELECT datname,count(*) FROM pg_stat_activity WHERE backend_type='client backend' GROUP BY datname;"
lsof -nP -iTCP:40405 | grep ESTABLISHED | awk '{print $2}' | sort | uniq -c   # conn al daemon
```

---

## Sessione 2026-06-17 — sticky-within-group attraverso il proxy

### Cosa si misura

Il proxy sticky (`GenropyStickyProxy`, contrib) ora pinna ogni connessione a un
worker del channel e distribuisce le connessioni su tutti i worker del gruppo
fino a una `capacity` per worker (prima mandava tutto al primo worker:
`allocations[0]`). Questa sessione verifica end-to-end che la distribuzione
**scali** rispetto al singolo processo, e quanto **costa** l'hop del proxy.

Setup come le sessioni precedenti (istanza `test_invoice_pg`, pagina `GET /`,
`hey`), due tracce a parità di daemon:
- **DIRECT**: un solo `gnrwsgiserve` (no proxy), `hey -c N`. Un processo → un GIL.
- **PROXY**: `gnrstickyserve --standard N --capacity 1`, `hey -c N`. Le N
  connessioni keep-alive di `hey` = N chiavi di affinità distinte (la chiave è
  `scope['client']`, host:porta, senza cookie) → una per worker.

### Prerequisito scoperto: il daemon in :40405 era il VECCHIO Pyro4

Primo run **inquinato**: ~metà richieste in `500`, già a `-c 1`. Causa
verificata leggendo il codice (`gnr.web.daemon.handler.GnrDaemon`): il daemon
attivo era ancora **Pyro4** (`Pyro4.Daemon`/`Pyro4.Proxy`, serializer pickle), non
il msgpack. Errore del worker: `[Errno 49] Can't assign requested address` —
**esaurimento porte effimere** sotto burst, l'identico artefatto della sessione
2026-06-16 (Test 1). Il `msgpack._cmsgpack.so` caricato nel processo era una dep
trasversale, non il transport: dedurre "nuovo" dalla sua presenza è stato un
errore (la verifica corretta è leggere `handler.py`).

Risolto installando il daemon nuovo (repo `genro-daemon`, `pip install -e .` →
rimpiazza `gnr.web.daemon`, drop-in) e avviandolo sulla porta che il sito
contatta (`siteconfig['gnrdaemon?port'] = 40405`): `gnr web daemon -P 40405`.
Verificato che il processo NON carica Pyro4 e carica `uvloop` + `msgpack`.
**Col daemon msgpack: 0 errori, tutte le richieste 200.**

### Numeri (daemon msgpack, tutte le risposte 200, 0 errori)

| concorrenza | DIRECT (1 worker, no proxy) | PROXY sticky `--standard N --capacity 1` | worker su daemon |
|---|---|---|---|
| N=1 | 53 req/s | 47 req/s | 2 |
| N=2 | 66 req/s | 75 req/s | 3 |
| N=4 | 61 req/s | 116 req/s | 5 |
| N=8 | 57 req/s | **134 req/s** | 9 |

### Conclusioni

1. **La baseline DIRECT è piatta a ~60 req/s** a ogni concorrenza (53→66→61→57):
   conferma il tetto del singolo GIL già misurato, indipendente dal numero di
   client.
2. **Il proxy sticky SCALA**: 47→75→116→134. Lo sticky distribuisce le N
   connessioni su N worker distinti (la colonna "worker su daemon" cresce
   2→3→5→9: 1 reception/proxy + N worker), superando il muro del singolo GIL.
3. **Costo dell'hop**: a N=1 (nessuna distribuzione possibile) il proxy fa 47 vs
   53 diretto, ~**11% di overhead** per l'hop httpx proxy→worker. Da N=2 in su la
   distribuzione vince sull'overhead: a N=8 il proxy fa **134 vs 57**, **2,35×**.
4. **Plafond sul daemon condiviso**: il proxy non raggiunge 60×N (a N=8 è 134,
   non 480) e la latenza in coda cresce (slowest ~2,2 s). Tutti i worker
   convergono sull'unico daemon :40405 — è il muro finale già previsto (sessione
   2026-06-16, conclusione 4). Qui un po' sotto il ~166 di allora per l'hop del
   proxy in più e perché i worker sono il dev server `gnrwsgiserve`.

La feature fa ciò per cui è nata: trasforma N worker fratelli con
load-balancing cieco in N worker indirizzabili per affinità, e il throughput
scala con essi fino al collo del daemon condiviso.

### Comandi (riproducibilità)

```bash
# daemon nuovo (msgpack) sulla porta del sito
pip install -e /path/to/genro-daemon         # rimpiazza gnr.web.daemon (drop-in)
gnr web daemon -P 40405

# proxy sticky, una connessione per worker
gnrstickyserve test_invoice_pg -p 8090 --standard 4 --capacity 1
hey -n 800 -c 4 http://127.0.0.1:8090/

# quanti worker sono attaccati al daemon
lsof -nP -iTCP:40405 | grep ESTABLISHED | awk '{print $2}' | sort -u | wc -l
```

## Sessione 2026-06-20 — PASSO ZERO: site register in-process (daemon-less)

### Cosa si misura

Il collo identificato il 2026-06-17 è lo **stato condiviso serializzato sul daemon**. Il
PASSO ZERO della transizione daemon-less porta il `GnrSiteRegister` *dentro* il processo
worker (zero rete), selezionabile via config (`gnrdaemon mode="inprocess"` vs `"daemon"`).
Implementazione: `genro-daemon` espone `InProcessSiteRegisterClient` (stesso `GnrSiteRegister`
del daemon, istanziato locale, `backend=None`, Bag vero al posto del `RemoteStoreBag` proxy) e
un singolo RLock per-sito (`critical_section()`) che serializza ogni operazione come faceva il
daemon mono-thread; `genropy` sceglie il client tramite `make_site_register_client`.

Questo test confronta **lo stesso sito** (`test_invoice_pg`, Postgres, mainpackage `invc`),
stessa porta, stesso carico: unica variabile = register via **daemon TCP/msgpack** vs
**in-process**. Endpoint home `/`, keep-alive, `hey`. Un ciclo richiesta = **6 chiamate al
register** (`new_connection`, `new_page`, `lock_item`, `get_item`, `unlock_item`,
`handle_ping`): nel daemon sono 6 round-trip msgpack, in-process 6 chiamate dirette in RAM.

### Numeri (tutte le risposte 200, 0 errori, dev server `gnrwsgiserve` 1-proc)

| carico | daemon req/s | in-process req/s | Δ throughput | daemon lat. media | in-process lat. media |
|---|---|---|---|---|---|
| c=10, n=2000 | 60.9 | **83.4** | **+37%** | 164ms | **119ms** (−27%) |
| c=50, n=3000 | 58.9 | **79.3** | **+35%** | 846ms | **628ms** (−26%) |

Percentili: c=10 daemon p99 228ms → in-process p99 172ms; c=50 daemon p99 1010ms →
in-process p99 758ms.

### Conclusioni

1. **Guadagno reale e costante**: +35-37% req/s e −26-27% latenza, solo togliendo il
   round-trip al daemon. Misurato sul percorso home dove il register è una frazione del costo
   (c'è ORM + rendering): su endpoint register-intensive (molte setItem/getItem sul serverstore)
   il delta sarebbe più marcato.
2. **Entrambe le modalità saturano a un plateau** (daemon ~59-61, in-process ~79-83): alzando i
   client la latenza cresce ma il throughput resta piatto. È lo stesso processo WSGI mono sotto
   (GIL singolo); il register in-process gira sotto il lock unico. Il plateau in-process è però
   **~35% più alto** del daemon.
3. **Il salto vero arriva dopo**: questo è UNA istanza. Ai passi 1-3 (N worker sticky, ognuno col
   suo register in-process) spariscono sia il round-trip sia la serializzazione condivisa sul
   daemon → scala con i worker, cosa che il daemon condiviso non permetteva (era il muro: 8
   worker → 134-150 invece di ~480, sez. 2026-06-17).
4. **Legacy intatto**: `mode` assente o `"daemon"` mantiene il comportamento attuale; il flag
   `thread_safe=False` di default rende il register bit-identico per il daemon. Le due modalità
   coesistono sullo stesso host (verificato: `test_invoice_pg` in-process e daemon :40405 attivo
   per altri siti).

### Comandi (riproducibilità)

```bash
# daemon msgpack attivo su :40405 (resta acceso, serve altri siti)
# sito di test: projects/test_invoice/sites/test_invoice_pg (punta all'istanza Postgres omonima)

# run daemon: siteconfig <gnrdaemon mode="daemon"/>
gnrwsgiserve test_invoice_pg -p 8099 --noreload --nodebug
hey -n 2000 -c 10 -disable-keepalive=false http://127.0.0.1:8099/

# run in-process: siteconfig <gnrdaemon mode="inprocess"/> (nessun contatto col daemon)
gnrwsgiserve test_invoice_pg -p 8099 --noreload --nodebug
hey -n 2000 -c 10 -disable-keepalive=false http://127.0.0.1:8099/
```

## 2026-06-21 — Realistic A1 replay benchmark (in-process vs daemon)

Replaced the synthetic `GET /` load with a **faithful replay of a captured browser
session** (modo A1). A real session on `test_invoice_pg` was recorded through a logging
proxy (`temp/benchmark_assets/session_capture.jsonl`, 164 reqs, 93 pageCalls, all 200),
then replayed by `temp/benchmark_assets/replay_a1.py`.

**Model (mirrors the browser):** one HTTP session (cookie jar) per simulated user = one
server connection. The frame page (`GET /`) only authenticates; the real work lives in TH
pages opened as iframes (`/sys/thpage/invc/<table>`), each with its own server-minted
`page_id`. Per user: `GET /` → login → for each of 5 TH pages (customer, invoice, product,
postcode, lookup): `GET` the iframe (extract `page_id` from `page_id:'…'` in the HTML) →
`POST main` → `POST app.getSelection` ×N. `page_id`/`callcounter` re-minted per user; all
other form values verbatim from the capture, so the server works on real contexts. A1 =
heavy reads only (`main` + `app.getSelection`); writes excluded (would need per-session
pkeys). ~34 reqs/round/user.

Now running on the **fixed register** (`genro-inprocess-register`, the dangling
`_subscribed_table_index` bug closed): every run below is 0 errors, 0 non-200, 0 empty
selections.

| users (×3 rounds) | in-process req/s | daemon req/s | Δ in-process | p90 in-proc / daemon |
|---|---|---|---|---|
| 5  | 120.8 | 100.6 | +20% | 64ms / 86ms |
| 10 | 118.3 | 103.8 | +14% | 136ms / 166ms |
| 20 | 117.0 | 101.6 | +15% | 282ms / 329ms |

### Findings

1. **In-process is ~15-20% faster on a realistic mixed read load**, lower than the synthetic
   `GET /` delta (~35%) because here each request carries real ORM + selection work, so the
   register round-trip is a smaller fraction of the total. The advantage is consistent across
   concurrency levels and shows in both throughput and tail latency (p90).
2. **Both plateau (~117 in-process, ~102 daemon)**: single WSGI process under the GIL; the
   in-process register runs under its single per-site lock. The plateau is just shifted up.
3. **The real gain is still ahead**: this is ONE instance. With N sticky workers (each with
   its own in-process register) both the TCP round-trip and the shared serialization on the
   daemon disappear — the path the daemon could not scale (sez. 2026-06-17).
4. **Faithful, not synthetic**: replay reuses captured form values on server-minted page_ids,
   so `app.getSelection` runs the real query/selection (responses up to ~45 KB, never empty).

### Comandi (riproducibilità)

```bash
# capture (one-off): logging proxy in front of :8099, browse a real session
python3 temp/benchmark_assets/capture_proxy.py        # :8090 -> :8099, writes session_capture.jsonl

# replay (per mode: switch siteconfig <gnrdaemon mode="…"/> and restart :8099)
python3 temp/benchmark_assets/replay_a1.py --users 20 --rounds 3 --base http://127.0.0.1:8099
```

## 2026-06-21 — gunicorn grid + register-call count (the real finding)

Two corrections to earlier same-day numbers:
- The first A1 numbers (~+15-20% in-process) ran on **Werkzeug** (dev server, GIL-bound) —
  NOT a valid measurement. Disregard them.
- Re-run on **gunicorn** (`gnr web serveprod ... -k gthread`), 1 worker, threads x users grid
  ({2,4,8,16} x {4,8,16,32}, 10s/cell). Both modes are **flat ~75-83 req/s**, insensitive to
  threads and users. On a single worker in-process ≈ daemon (daemon a hair higher, within
  noise). Threads do not raise throughput (CPU-bound request under the GIL); they only raise
  latency (p90 90→690ms as users grow).

### Register is hit ~56 times PER HTTP REQUEST (measured)

A print at the daemon's server-side dispatcher (`ars.py`, after `_req_parse`) counted every
call reaching the daemon during one A1 round (1 user, 35 HTTP requests, daemon mode on an
isolated instrumented daemon :40410):

**1960 daemon calls / 35 requests ≈ 56 register calls per request.** Breakdown:

| daemon method | calls | role |
|---|---|---|
| `remotebag_getItem` | 819 | serverstore Bag reads |
| `get_item` | 811 | register item fetch |
| `lock_item` / `unlock_item` | 72 / 72 | per-operation lock |
| `get_dbenv` | 40 | db env from register |
| `remotebag_setItem` | 38 | serverstore Bag writes |
| `subscription_storechanges` | 29 | external-changes pull (one per pageCall — happens always) |
| `pages` | 29 | page enumeration |
| new_page / setPendingContext / remotebag_update / setInClientData / … | ~50 | misc |

83% are `remotebag_getItem` + `get_item` (Bag/item access). `subscription_storechanges`
fires ~once per pageCall regardless of what the call does — the register is consulted on
*every* rpc, even a trivial one.

**Why client-side probes read 0** (recorded so the mistake is not repeated): in daemon mode
`subscription_storechanges`, `get_item`, etc. execute **inside the daemon process**, not the
worker. Hooking `_sr_call` / `_invoke_method` / `_send` / `subscription_storechanges` in the
worker showed 0 because those calls are dispatched from a code path the worker-side hooks did
not intercept; the daemon's own dispatcher is the only place that sees all of them.

### Implication for daemon-less

The earlier "register barely touched, so the modes tie" reading was **wrong**. The register
is hammered ~56×/request. In daemon mode each of those is msgpack serialize + TCP round-trip +
lock; in-process they become direct in-RAM calls under the single per-site lock. At 1 worker
the cost is masked (loopback TCP is cheap in latency, GIL/ORM dominate), so throughput ties —
but those 56 round-trips/request all converge on the **single-threaded daemon**, which is the
wall under multiple workers. The daemon-less value is not "a few % faster" — it is removing
~56 serialize+round-trips per request, and unblocking per-worker scaling that the shared
daemon caps.

**Next experiment (the one that proves it):** N-worker gunicorn (sync, 1/2/4/8 workers),
in-process vs daemon, with sticky routing (in-process register is per-process). Expectation:
in-process scales ~linearly with workers; daemon plateaus on the shared bottleneck.

### Why the aggregate throughput shows no gap (and where the gap actually is)

The puzzle: if the daemon does ~56 TCP round-trips per request, removing them must cost
*something* — yet 1-worker throughput ties (~80 req/s either way). Resolved by measuring a
**single register call in isolation**, outside HTTP/GIL/ORM noise (5000 iterations of
`get_item`, in-process = direct RAM call under `critical_section`; daemon = same call over
loopback TCP + msgpack to the live daemon):

| | per call | per request (×56) |
|---|---|---|
| in-process (RAM) | **0.60 µs** | ~0.03 ms |
| daemon (loopback TCP + msgpack) | **43.20 µs** | **~2.4 ms** |
| ratio | **72× slower via daemon** | |

So the gap is **real and large per call** (72×), but ~2.4 ms/request disappears inside a
~15-20 ms request dominated by the DB query + Bag rendering. The 1-worker throughput ceiling
is set by the **GIL/ORM (~80 req/s)**, not by register I/O — shaving 2.4 ms off a 15 ms
request does not raise a ceiling that sits at 15 ms. The advantage is masked at the aggregate,
not absent.

Where it becomes visible:
- **register-intensive requests** — a page reading a large Bag key-by-key (e.g. 500
  `remotebag_getItem`) pays 500 × 43 µs ≈ **21 ms** of pure transport via daemon, ~0 in-process.
- **N workers** — all those 43-µs calls converge on the **single-threaded daemon**; it
  saturates while per-process in-process registers scale independently.

This isolated micro-benchmark — not the aggregate throughput — is the clean number that
demonstrates the daemon-less value.

### Why in-process is already optimal (Bag-by-reference) and still correct

The 819 `remotebag_getItem`/request in daemon mode are **not lock contention** — they are the
`RemoteStoreBag` **network proxy** being read key-by-key, one round-trip per Bag node (an N+1
of the *transport*, not the logic). `get_item(include_data=...)` is the switch: `False` returns
metadata only (no Bag touch); `True` attaches `item["data"]`, and there the two modes diverge:
- **daemon** (`_add_data_to_register_item`, siteregister_client.py:437): `item["data"] =
  RemoteStoreBag(...)` — a lazy network proxy; every later `getItem` is a round-trip → the 819.
- **in-process** (override, siteregister_client.py:517): `item["data"] =
  get_item_data(...)` → the **live in-RAM Bag by reference** (`itemsData.get(id)`,
  siteregister_base.py:264, returns the live Bag, not a copy). Later reads are direct memory
  access — **not register calls at all**. The N+1 is removed at the root, not merely sped up.

So in-process is already the most efficient form (zero-copy, no residual N+1); there is no
optimization left on the table on the in-process side.

Correctness of the shared live Bag under gthread (multiple threads on the same page): writes
happen inside the consumer's `with pageStore() as store:` context manager. `ServerStore.__enter__`
takes a **per-page `lock_item`** (siteregister_client.py:40-63) with retry+backoff and a timeout
(`GnrDaemonLocked`), `__exit__` releases it (line 65-70). So two gthread threads on the same page
**serialize on that per-page lock** — no single-threaded worker required, and no per-site lock
needed for store mutations. The earlier worry "live Bag mutated outside the lock" was wrong: it
is framed by the context manager. The old "slow call blocks the page" failure mode lives here,
not in `critical_section` — but the `with pageStore()` body is kept short by the **application
developer** (RAM mutations only, never slow I/O inside it), and this is unchanged from the daemon
(the per-page lock and context manager predate the in-process mode).

### Method note (so the day's mistakes aren't repeated)

Client-side probes (`_sr_call`, `_invoke_method`, `_send`, `subscription_storechanges` hooked in
the worker) all read **0** and led to a wrong "register barely touched" conclusion. In daemon
mode those methods execute **inside the daemon process**, not the worker. The reliable place to
count register traffic is the **daemon's server-side dispatcher** (`ars.py`, after `_req_parse`)
— the funnel every call must cross — not the many client-side entry points. Measure at the
funnel, not at the taps; and remember that in daemon mode the register lives in another process.
