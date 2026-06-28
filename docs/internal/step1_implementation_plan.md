# Step 1 — Piano di implementazione (commander + sticky-per-user)

**Version**: 0.1.0
**Status**: 🔴 DA REVISIONARE
**Last Updated**: 2026-06-24

> Piano eseguibile dello Step 1. Il ragionamento e il ciclo di vita stanno in
> [step1_spawner_design.md](step1_spawner_design.md); i fatti sul daemon in
> [legacy_daemon_role.md](legacy_daemon_role.md). Tutti i file:riga sono stati
> verificati nel codice durante la stesura. Lingua: italiano (doc interno).

---

# Piano di implementazione — Step 1: commander + sticky-per-user (HTTP-only, sopra il daemon)

Stato: 🔴 DA REVISIONARE. Branch corrente `feature/poc-sticky-workers`: il codice sticky già presente (routing per channel/xgroup) è **scrap** — riscrivere pulito. Riuso esplicito solo del `worker_orchestrator.py` (spawn/supervise) e del `GenropyProxy` (intatto). Tutti i test girano contro `test_invoice_pg` con `PGGSSENCMODE=disable`, login `amelia.martin/a`, carico con `capacity_bench_record.py`.

## Principio architetturale di partenza

Due livelli di processo, minimo due processi:

- **Processo MAIN**: `AsgiServer` multi-app standard, su cui è montata la `WorkerCommanderApplication`. Il commander NON esegue lavoro GenroPy: solo orchestrazione, decodifica cookie, routing/forward HTTP, registro per-utente. È il punto di ingresso pubblico.
- **Processo WORKER** (N istanze): `GenroAsgiWorker`, server genro-asgi minimale, processo uvicorn proprio, ospita **esattamente un** `GenropyProxy` su un solo site GenroPy. Il primo worker fa anche da welcome/login con capacità ridotta (la capacità è label dell'orchestrator, NON logica dentro il worker).

Vincolo verificato che guida le scelte: `GenropyProxy` non tocca mai `self.server` (nessun `server.*`, `request_registry`, `authenticate`). Quindi il worker è uno shell ASGI generico, app-agnostico, e vive in `src/genro_asgi/server/worker.py`; il wiring GenroPy (istanziare il proxy, spawnare i worker, comandarli) vive in `contrib/genropy_asgi/`.

---

## FASE 0 — Pulizia del branch e baseline (PRIMO CHUNK DA COMMIT)

**Obiettivo**: ripartire da un branch pulito senza il codice sticky-per-channel, conservando `worker_orchestrator.py` e `GenropyProxy`. Avere una baseline verde da cui costruire.

**Task**:
1. Identificare con `git log`/`git diff main...feature/poc-sticky-workers` esattamente cosa è codice sticky-per-channel (routing per xgroup, `WorkerGroup.capacity` esposto nella grammar/CLI, `gnrstickyserve`) e cosa è `worker_orchestrator.py` (da tenere).
2. Rimuovere il codice di routing sticky-per-channel e i suoi punti di innesto nella config grammar / CLI, lasciando intatto `worker_orchestrator.py` (Orchestrator/LocalOrchestrator/WorkerJob/WorkerGroup/Allocation) e `GenropyProxy`.
3. `pytest` + `ruff check .` verdi sul branch ripulito.

**Verifica prima di proseguire**: suite verde; `git status` mostra solo rimozioni del materiale sticky-channel; `worker_orchestrator.py` invariato.

**Riuso vs riscrittura**: riuso `worker_orchestrator.py` e `GenropyProxy`; rimozione di tutto il resto dello sticky-per-channel.

> **Questo è il primo commit-sized chunk**: `chore(contrib): drop sticky-per-channel scaffolding, keep orchestrator`. Niente feature nuove: solo terra bruciata controllata + baseline verde.

---

## FASE 1 — `GenroAsgiWorker`: il server minimale in genro-asgi src

**Obiettivo**: un server ASGI minimale, HTTP-only, single-app, che esegue un `GenropyProxy` su `test_invoice_pg` nel proprio processo uvicorn. Smoke: un worker risponde `curl` home → 200.

**Decisione di design (vincolante)**: `GenroAsgiWorker` è una **classe separata**, NON sottoclasse di `AsgiServer`. Motivo verificato: `AsgiServer.__init__` (server.py:117-145) cabla rigidamente `MemorySessionStore`, `LocalStorage`, `ResourceLoader`, `BasicAuthMixin`, `RequestRegistry`, `WsxHandler`, `ServerApplication` montata come `_server`, e il boot via `ConfigurationHandler`/`config.py`. Una sottoclasse ri-eseguirebbe tutto questo. Il worker riscrive il proprio `__init__`, `__call__`, `run`, **riusando** solo `Dispatcher` e `ServerLifespan`.

**KEEP (riuso diretto delle classi esistenti)**:
- `Dispatcher` (dispatcher.py:38-91) — riuso intatto. Con un solo app il `_resolve_mount` + `apps.get(mount) or apps.get('')` (dispatcher.py:83) collassa su "sempre l'unico app". Mantiene l'header-prep `scope['_headers']` (dispatcher.py:76-80), che resta utile.
- `ServerLifespan` (lifespan.py:68-189) — riuso intatto. Itera `self.server.apps` (lifespan.py:143, 162) e legge `self.server.logger`. È ciò che invoca `GenropyProxy.on_shutdown()` → `site.on_site_stop()` (genropy_proxy.py:104-107).
- `attach_instance` (RoutingClass, server.py:262) — riuso per stabilire la relazione duale `app.server -> worker`, anche se il proxy non legge mai `self.server`. È il contratto di mount.
- `run()` non-reload: `uvicorn.run(self, host, port)` (modello server.py:489). NIENTE branch reload, NIENTE `server_factory`.

**DROP (non presenti nel worker)**: apps come registry multi-app + `_mount_app`/`mount()` + `_apply_applications` + guard "mount already taken"; `ServerApplication`/`_server`; `WsxHandler` e il branch websocket di `__call__`; sistema middleware_chain + `_chain`; `BasicAuthMixin`; `RequestRegistry`; tutta l'orchestrazione `config.py`/`ConfigurationHandler`/`AsgiConfigBuilder`/famiglia `_apply_*`; `ResourceLoader`; `LocalStorage`/`StorageNode`; `MemorySessionStore`; `db_registry`/`build_db_handler`/`_apply_databases`; persistenza plugin config.

**Scelta a rischio risolta — `apps` come dict a 1 entry**: per non toccare `Dispatcher` né `ServerLifespan` (che iterano `self.server.apps`), il worker espone `self.apps` come dict a **una sola entry sul mount vuoto `''`**. Confermato: `GenropyProxy.mount_name = ''` mappa `default_uri` a `/` (genropy_proxy.py:73-74), e il fallback del dispatcher `apps.get('')` (dispatcher.py:83) colpisce sempre l'unico app. Il worker deve esporre anche `.logger`.

**`__call__` ridotto a due rami** (modello server.py:447-464, senza websocket):
```
if scope["type"] == "lifespan":   await self.lifespan(...)
else:                              await self.dispatcher(...)
```

**Task**:
1. Scrivere `src/genro_asgi/server/worker.py` con la classe `GenroAsgiWorker(host, port, app)`: `__init__` istanzia `self.logger`, `self.lifespan = ServerLifespan(self)`, `self.dispatcher = Dispatcher(self)`, `self.apps = {'': app}`, e fa `attach_instance` dell'app (per cablare `app.server`). `__call__` a due rami. `run()` = `uvicorn.run(self, host, port)`.
2. Esportare `GenroAsgiWorker` da `genro_asgi.__init__`.
3. Entry point GenroPy in contrib (`contrib/genropy_asgi/.../worker_entry.py` o `python -m genropy_asgi.worker`): legge `site`, `-p port`, flag `--debug/--nodebug`; istanzia `GnrWsgiSite`, lo avvolge in `GenropyProxy(gnr_site=site, debug=...)`, setta `mount_name = ''`, costruisce `GenroAsgiWorker(host, port, proxy)`, chiama `run()`.

**Rischi da gestire in questa fase**:
- **Boot ~secondi**: l'avvio di un worker (creazione `GnrWsgiSite`) richiede secondi → da mitigare con pre-spawn nelle fasi successive; qui va solo **misurato** il tempo di boot per dimensionare `READY_TIMEOUT`.
- **Werkzeug debugger**: i worker spawnati devono ricevere `debug=False` (l'orchestrator oggi passa `--nodebug`, worker_orchestrator.py:288). L'entry point del worker deve propagare lo stesso flag a `GenropyProxy`, altrimenti booterebbe con il debugger interattivo esposto.
- **Shutdown via segnale**: `on_site_stop` gira via `ServerLifespan.shutdown` su `lifespan.shutdown` graceful. Verificare che uvicorn traduca il SIGTERM (che l'orchestrator manda al process group, worker_orchestrator.py:307) in `lifespan.shutdown`. Backstop: `atexit.register(site.on_site_stop)` esiste solo nel path `_create_site` (genropy_proxy.py:142), NON quando si passa `gnr_site` pre-costruito — quindi in questa fase non c'è rete di sicurezza se uvicorn non fa il graceful shutdown. Da verificare esplicitamente.

**Verifica prima di proseguire (test indipendente)**:
- Avviare a mano l'entry point: `PGGSSENCMODE=disable python -m genropy_asgi.worker test_invoice_pg -p <free> --nodebug`.
- `curl -i http://127.0.0.1:<free>/` → 200 (home), e una rotta che richiede DB → 200 (prova che il proxy esegue davvero il site).
- SIGTERM al processo → nei log compare `on_site_stop` (graceful shutdown verificato).

**Riuso vs riscrittura**: riuso `Dispatcher`, `ServerLifespan`, `GenropyProxy`, `attach_instance`. Riscrittura: `worker.py` (`__init__`/`__call__`/`run` nuovi, ~40 righe) + entry point contrib.

---

## FASE 2 — Commander skeleton: spawn/supervise + forward HTTP

**Obiettivo**: `WorkerCommanderApplication` (AsgiApplication montata sul server MAIN) che spawna e supervisiona N worker via orchestrator, mantiene il `children` dict, inoltra una richiesta HTTP a un worker e ne rilancia la risposta. Ancora **nessuno** sticky: routing fisso (es. round-robin o "sempre il primo running") per provare il tubo end-to-end.

**Adattamento orchestrator (punto di integrazione reale)**: oggi `LocalOrchestrator._start_worker` (worker_orchestrator.py:275-291) spawna `gnrwsgiserve` (il server WSGI legacy). Va sostituito lo spawn con `python -m genropy_asgi.worker <site> -p <port> -H <host> --nodebug` (il `GenroAsgiWorker` della Fase 1). `start_new_session=True` e il kill di gruppo (worker_orchestrator.py:289, 297-318) restano per sicurezza, anche se un worker uvicorn normalmente non genera figli.

**`children` / `Allocation`**: il commander legge `orchestrator.allocations()` che restituisce `Allocation(id, group, host, port, status)`. Lo `children` dict del commander è `alloc_id -> Allocation`; nessuno stato per-utente ancora.

**Forward HTTP**: il commander, dato un `Allocation` target, apre una richiesta HTTP al worker (`host:port`), inoltra metodo/path/headers/body presi da `scope`/`receive`, e rilancia status/headers/body via `send`. HTTP-only: nessun branch websocket (allineato a `GenroAsgiWorker.__call__` a due rami).

**Task**:
1. `contrib/genropy_asgi/.../worker_commander.py`: `WorkerCommanderApplication(AsgiApplication)` con `on_startup` che costruisce il `WorkerJob` (site `test_invoice_pg`, gruppi `welcome` count=1 + `pool` count=1 minimo) e chiama `orchestrator.register(job)`; `on_shutdown` che chiama `orchestrator.stop()`.
2. `handle_request` (override, come fa GenropyProxy): scegli un `Allocation` running con policy banale, fai il forward HTTP, rilancia la risposta.
3. Montare il commander sul server MAIN (config.py del MAIN che monta `WorkerCommanderApplication` sul mount vuoto).

**Rischi**: pre-spawn dei worker in `on_startup` per assorbire il boot ~secondi prima di accettare traffico. `READY_TIMEOUT` dimensionato sul boot misurato in Fase 1.

**Verifica prima di proseguire (test indipendente)**:
- Avviare il MAIN; in `on_startup` partono ≥2 worker (welcome + pool), entrambi `running`.
- `curl -i http://127.0.0.1:<main>/` → 200, servito da un worker (verificabile da log/port nel worker).
- Uccidere a mano un worker → la supervisione (worker_orchestrator.py:223-247) lo rilancia entro pochi secondi, `allocations()` torna a count.

**Riuso vs riscrittura**: riuso pieno di `Orchestrator`/`LocalOrchestrator`/`WorkerJob`/`WorkerGroup`/`Allocation` (solo `_start_worker` cambia il comando spawnato). Riscrittura: `worker_commander.py` (skeleton).

---

## FASE 3 — Registro per-utente + sticky routing (decode cookie)

**Obiettivo**: instradare per utente. Guest → welcome worker; loggato → `worker_attuale` assegnato al primo login. Decodifica cookie HMAC senza I/O.

**Chiavi di routing**:
- Guest (nessuna identità nel cookie) → chiave `cid:<...>` → welcome worker.
- Loggato → chiave `usr:<username>` → `worker_attuale` dal registro; assegnazione alla prima richiesta loggata.

**Decodifica cookie**: HMAC, nessun I/O. Estrarre l'username dal cookie di sessione GenroPy.

> **RISCHIO MULTIDOMINIO DA GESTIRE QUI (flag esplicito)**: il nome del cookie cambia tra dominio singolo e multidominio (decode sotto `'site'` vs `currentDomainIdentifier`). La logica di decode deve gestire entrambi i casi, altrimenti in multidominio l'utente loggato viene visto come guest e mandato al welcome worker. Verificare il nome effettivo del cookie nel site `test_invoice_pg` prima di cablare la chiave `usr:`. Questo è il punto fragile della fase: testarlo per primo.

**Registro per-utente** (nello spawner/commander): `usr:<username> -> {worker_attuale, worker_target, inflight}`. In questa fase `worker_target` resta `None` e `inflight` non è ancora popolato (arriva in Fase 5). Qui serve solo `worker_attuale`.

**Welcome worker a capacità ridotta**: il gruppo `welcome` ha `capacity` ridotta (riserva posti per i guest); è attributo dichiarativo di `WorkerGroup` (worker_orchestrator.py:55-60), onorato dal commander nel routing, NON dal worker. Il worker è identico a prescindere dal ruolo.

**Task**:
1. Funzione di decode cookie (HMAC, no I/O) che restituisce `username | None`, robusta al caso multidominio.
2. Registro per-utente nel commander: assegnazione `worker_attuale` al primo login (scegli un `pool` worker; in questa fase il primo running sotto capacità).
3. `handle_request` del commander: se guest → welcome; se loggato → `worker_attuale` (assegna se assente). Forward come Fase 2.

**Verifica prima di proseguire (test indipendente)**:
- `curl` senza cookie → servito dal welcome worker.
- Login `amelia.martin/a` su `test_invoice_pg`: la prima richiesta loggata assegna `usr:amelia.martin -> pool worker X`; richieste successive con lo stesso cookie tornano sempre sul worker X (verificabile da log/port).
- (Multidominio) ripetere con il site configurato multidominio e confermare che `amelia.martin` NON viene visto come guest.

**Riuso vs riscrittura**: riscrittura della logica di routing e del registro nel commander; riuso del forward HTTP della Fase 2.

---

## FASE 4 — Capacità + spawn elastico

**Obiettivo**: ogni worker `pool` ha un numero fisso di utenti (es. 6); early-spawn alla soglia (es. 4); nuovi utenti loggati vanno a un worker sotto capacità; welcome a capacità ridotta.

**Task**:
1. Contare gli utenti distinti assegnati per worker (dal registro per-utente). Capacità presa da `WorkerGroup.capacity`.
2. Assegnazione nuovo utente loggato: scegli un `pool` worker sotto `capacity`.
3. Early-spawn: quando un worker raggiunge la soglia (es. 4 su 6), chiamare `orchestrator.scale('pool', count+1)` per avere già pronto il prossimo worker prima di saturare (assorbe il boot ~secondi).

**Rischio (flag)**: boot-latency — l'early-spawn deve anticipare la saturazione di abbastanza margine (soglia 4 su cap 6) perché il nuovo worker sia `running` prima che serva. La capacità resta logica del commander/orchestrator, MAI dentro il worker.

**Verifica prima di proseguire (test indipendente)**:
- `capacity_bench_record.py` con abbastanza utenti distinti da superare la soglia: verificare che alla soglia parta un nuovo `pool` worker e che il (soglia+1)-esimo utente venga assegnato al nuovo worker, non al saturo.
- Nessun utente assegnato oltre `capacity` su un worker.

**Riuso vs riscrittura**: riuso `orchestrator.scale` (worker_orchestrator.py:145-170) e `WorkerGroup.capacity`. Riscrittura: politica di assegnazione e trigger di early-spawn nel commander.

---

## FASE 5 — inflight tracking + migrazione opportunistica

**Obiettivo**: migrazione per-utente lazy e opportunistica. `inflight` è un **dict di richieste indicizzato per `request.id`**, NON un contatore. Si segna `worker_target`; le nuove richieste continuano su `worker_attuale`; lo switch avviene quando l'utente è quiescente (`inflight` vuoto).

**Task**:
1. Popolare `inflight[request.id]` al forward e rimuoverlo alla risposta completata (try/finally attorno al forward HTTP).
2. API di migrazione: marca `worker_target` per un `usr:<username>` (senza spostare nulla subito).
3. Allo svuotarsi di `inflight` per quell'utente: promuovi `worker_target` → `worker_attuale`, azzera `worker_target`. Da quel momento le nuove richieste vanno al nuovo worker.

**Verifica prima di proseguire (test indipendente)**:
- Sotto carico `capacity_bench_record.py` su `usr:amelia.martin`: marcare `worker_target` mentre ci sono richieste inflight → le nuove richieste restano su `worker_attuale` finché `inflight` non si svuota, poi migrano in blocco. Nessuna richiesta inflight troncata dalla migrazione.
- `inflight` torna sempre vuoto a fine carico (nessuna richiesta orfana lasciata nel dict).

**Riuso vs riscrittura**: riscrittura della logica inflight/migrazione nel registro per-utente del commander; riuso del forward HTTP delle fasi precedenti.

---

## Riepilogo riuso vs riscrittura

| Componente | Esito |
|---|---|
| `worker_orchestrator.py` (Orchestrator, LocalOrchestrator, WorkerJob, WorkerGroup, Allocation) | **RIUSO**, unica modifica: comando spawnato in `_start_worker` (gnrwsgiserve → `python -m genropy_asgi.worker`) |
| `GenropyProxy` | **RIUSO INTATTO** (zero modifiche, verificato) |
| `Dispatcher`, `ServerLifespan`, `attach_instance` | **RIUSO INTATTO** dal worker |
| Codice sticky-per-channel sul branch | **SCRAP** (rimosso in Fase 0) |
| `src/genro_asgi/server/worker.py` (`GenroAsgiWorker`) | **NUOVO** |
| entry point worker contrib | **NUOVO** |
| `WorkerCommanderApplication` (contrib) | **NUOVO** |

## Flag rischi (dove vanno affrontati)
- **Cookie multidominio** → Fase 3 (decode sotto `'site'` vs `currentDomainIdentifier`): da testare per primo nella fase.
- **Boot worker ~secondi** → mitigato da pre-spawn in Fase 2 (`on_startup`) e early-spawn in Fase 4; `READY_TIMEOUT` dimensionato sul boot misurato in Fase 1.
- **Shutdown graceful (SIGTERM → lifespan.shutdown)** → verificato in Fase 1; senza di esso `on_site_stop` non gira (niente atexit backstop con `gnr_site` pre-costruito).
- **Werkzeug debugger** → `--nodebug` propagato all'entry point del worker in Fase 1.

## Primo commit-sized chunk
**FASE 0**: ripulire il branch dal codice sticky-per-channel mantenendo `worker_orchestrator.py` e `GenropyProxy`, suite verde. Commit: `chore(contrib): drop sticky-per-channel scaffolding, keep orchestrator`. Solo dopo, Fase 1 (`GenroAsgiWorker`) come secondo chunk.

---

File rilevanti (path assoluti):
- `/Users/gporcari/Sviluppo/genro_ng/meta-genro-modules/sub-projects/genro-asgi/src/genro_asgi/server/server.py` (`__call__` 447-464, `run` 489, `__init__` 117-145)
- `/Users/gporcari/Sviluppo/genro_ng/meta-genro-modules/sub-projects/genro-asgi/src/genro_asgi/server/dispatcher.py` (header-prep 76-80, fallback 83)
- `/Users/gporcari/Sviluppo/genro_ng/meta-genro-modules/sub-projects/genro-asgi/src/genro_asgi/lifespan.py` (itera `apps` 143/162)
- `/Users/gporcari/Sviluppo/genro_ng/meta-genro-modules/sub-projects/genro-asgi/contrib/genropy_asgi/src/genropy_asgi/worker_orchestrator.py` (`_start_worker` 275-291, `scale` 145-170, `WorkerGroup.capacity` 55-60)
- `/Users/gporcari/Sviluppo/genro_ng/meta-genro-modules/sub-projects/genro-asgi/contrib/genropy_asgi/src/genropy_asgi/genropy_proxy.py` (intatto; `handle_request` 81-102, `on_shutdown` 104-107, `mount_name` 65-74)

Nuovi file da creare: `src/genro_asgi/server/worker.py`, entry point worker e `worker_commander.py` sotto `contrib/genropy_asgi/src/genropy_asgi/`.