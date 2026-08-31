# Il ciclo a otto core — bridge contro legacy, 120 utenti attivi

**Stato**: 🔴 DA REVISIONARE

Confronto a topologia fissa: otto core e quattro gibibyte per stack, otto processi
per stack, fino a centoventi utenti che chiamano una volta al secondo ciascuno,
con una pausa di cinquanta utenti e il loro rientro graduale.

## La differenza che conta: il ritmo è per utente

Il confronto L120 dichiarava un ritmo globale e distribuiva le richieste di quel
ritmo fra gli utenti. Quando un utente smetteva, gli altri assorbivano la sua
quota e il ritmo offerto non si muoveva.

Qui ogni utente attivo possiede una richiesta al secondo tutta sua, e il piano
semplicemente **non porta righe** per un utente in pausa. Il ritmo offerto è il
numero di utenti attivi: la pausa di cinquanta lo porta da 120/s a 70/s perché
mancano cinquanta utenti di lavoro, non perché un ritmo è stato riscritto.

La forma del ciclo sta quindi nel piano, non nel driver. Una riga esiste
esattamente quando il suo utente deve chiamare.

## Le otto fasi

| # | fase | durata | ritmo | cosa accade |
|---|---|---|---|---|
| 1 | `login_ramp` | 120 s | 0 → 119/s | un utente entra ogni secondo e chiama un secondo dopo |
| 2 | `full_stabilize` | 60 s | 120/s | tutti attivi. **Solo se i 120 sono stati raggiunti** |
| 3 | `full_measure_1` | 120 s | 120/s | la prima misura |
| 4 | `pause_50` | 60 s | 70/s | cinquanta smettono di chiamare, restano residenti |
| 5 | `return_ramp` | 51 s | 70 → 120/s | rientrano uno al secondo |
| 6 | `full_measure_2` | 120 s | 120/s | la seconda misura |
| 7 | `logout` | 120 s | — | logout, uno al secondo |
| 8 | `observe` | 120 s | — | nessun carico: memoria e processi |

La finestra del rientro dura **un secondo in più** dei cinquanta rientri: il primo
che rientra a t=0 chiama a t=1, quindi il ritmo pieno arriva un periodo dopo
l'ultimo rientro. Senza quel secondo la finestra chiuderebbe a 119/s e i 120/s
cadrebbero esattamente sul confine con la misura successiva.

Le fasi 4, 5 e 6 si giocano **solo se i centoventi utenti sono stati raggiunti**.
Con una popolazione fermata prima, la pausa artificiale di cinquanta misurerebbe
una forma che la corsa non ha mai avuto. Le fasi saltate sono registrate in
`phases_played`, mai silenziose.

## Le due guardie, che non vanno confuse

| | `MEMORY_STOP` | `ADMISSION_STOP` |
|---|---|---|
| che cosa osserva | `memory.current` contro il limite del cgroup | il p95 mobile delle chiamate applicative |
| dove vive | `bench_common/stop_guard.py` (condiviso) | `admission_guard.py` (di questo scenario) |
| effetto | **arresto completo**: alza la bandiera, ogni fase finisce | **arresto della crescita**: chiude la porta |
| gli utenti attivi | si fermano | continuano fino alla fine |
| reversibile | no | no, e non si riapre nemmeno se la latenza rientra |
| file | `*_memory_guard.json` | `*_admission.json` |

Un OOM e la perdita di identità del container sono arresti completi, come il
memory stop. Riportare le due cose con un solo numero renderebbe uno stack
sovraccarico indistinguibile da uno che ha finito la memoria, quindi le due non
condividono né un contatore, né un file, né una bandiera.

### Cosa osserva la guardia di ammissione

Solo **chiamate applicative reali**: la guardia è alimentata dalle chiamate
completate del motore di carico. Login, logout, census, letture
dell'orchestrazione e certificazione della page-class cache non passano da lì. Una
guardia che guardasse il census guarderebbe lo strumento.

Condizione: p95 delle chiamate completate negli **ultimi dieci secondi**,
ricalcolato **una volta al secondo**, **strettamente maggiore** di 1500 ms, per
**quindici valutazioni consecutive**. Servono almeno trenta campioni nella
finestra; sotto quella soglia la valutazione è *illeggibile* e non è né una
violazione né un azzeramento.

**Quanti secondi di lentezza sono davvero, misurato sulla guardia stessa**: la
finestra è mobile, quindi un secondo lento continua a violare per le dieci
valutazioni in cui resta dentro. Quindici violazioni consecutive richiedono
perciò **circa cinque secondi consecutivi** di lentezza, non quindici. Tre secondi
lenti producono dodici violazioni e la porta resta aperta; uno solo ne produce
dieci. Leggere "quindici valutazioni" come "quindici secondi" sovrastimerebbe
quanto la guardia tollera.

### Cosa fa quando scatta

Scrive subito l'evento su disco — una corsa uccisa un secondo dopo deve comunque
lasciare il fatto — con timestamp, fase, popolazione autenticata e attiva,
p50/p95/p99, completate, pendenti e motivo. Poi la porta resta chiusa per il resto
dell'esecuzione: nessun login, nessun rientro. Gli utenti già attivi lavorano fino
alla fine e la misura continua alla popolazione raggiunta.

Le righe del piano di un utente non ammesso vengono **trattenute e contate**
(`withheld` per fase): il piano è fisso, quindi ciò che la corsa non ha mandato
deve essere un numero.

## La configurazione

| | bridge | legacy |
|---|---|---|
| core | 8 | 8 |
| memoria | 4 GiB | 4 GiB |
| processi | 8 worker del pool | 8 worker Gunicorn |
| utenti per processo | `worker_max_users=15` | — |
| distribuzione attesa | `[15,15,15,15,15,15,15,15]` | — |
| riga di servizio | recipe `cycle_recipe.py` | `-w 8 -k gthread --threads 16` |

Sul bridge, per tutta la corsa:

- **`cpu_grow_percent=None`**: la crescita per pressione CPU non entra mai. Un
  worker nasce solo dalla domanda concreta di un placement che non trova posto
  altrove. Non c'è nessun apply a caldo: la policy della misura è nella recipe dal
  primo istante, quindi le due gambe condividono una linea del tempo senza attese
  di pareggio.
- **`worker_min_life_seconds=3600`**: controllo sperimentale, non un valore di
  produzione. Il retirement non deve poter chiudere un worker a metà ciclo.
- **freeze assente, cioè mai**. La pausa di sessanta secondi deve lasciare i
  cinquanta residenti sul loro worker: un freeze li porterebbe su disco e la pausa
  misurerebbe il congelatore invece della residenza. Il driver certifica che
  `user_idle_freeze_minutes` torni `null` prima di misurare qualsiasi cosa, e
  blocca la gamba se compare un solo utente congelato.

## Cosa è riusato e cosa è nuovo

Riusato da `bench_common`, senza copiarlo: il lifecycle del laboratorio
(`lab_lifecycle.sh`), il guardiano di memoria e la bandiera di stop
(`stop_guard.py`), i classificatori di processo e i campioni per ruolo
(`container_probe.py`), il motore di carico (`load_engine.py`), le letture del
bridge (`bridge_eyes.py`), la certificazione della page-class cache
(`page_class_cache.py`), il generatore dei piani (`make_plans.sh`).

Nuovo, e solo questo:

| file | cosa fa |
|---|---|
| `make_cycle_trace.py` | il generatore deterministico del piano per utente |
| `cycle_probe.py` | il driver delle otto fasi, con `CycleEngine` sopra `LoadEngine` |
| `admission_guard.py` | la guardia di latenza |
| `cycle_recipe.py` | la recipe del bridge: otto worker, freeze assente |
| `overrides/` | i due override del compose |
| `run_cycle.sh`, `run_smoke.sh` | il runner comparativo e lo smoke |
| `tests/test_cycle_tools.py` | i test mirati |

`CycleEngine` aggiunge tre cose al motore condiviso e non ne toglie nessuna: la
riga si offre solo a un utente ammesso; ogni chiamata completata va alla guardia;
la **prima** chiamata di un utente che rientra è cronometrata a parte, perché il
costo del ritorno non si vede in una media.

Le colonne del CSV di questo scenario sono quelle condivise più tre —
`users_active`, `users_paused`, `admission_stop` — e il formato dei CSV degli
scenari precedenti non è toccato: un test lo verifica.

## I piani

Non sono versionati: il piano pieno pesa 6,6 MB. Si rigenerano identici da
`plans.spec.json`, che porta il seed, gli argomenti esatti e l'hash atteso.

```bash
../bench_common/make_plans.sh .
```

| piano | utenti | in pausa | richieste | sha256 |
|---|---|---|---|---|
| `cycle_plan.json` | 120 | 50 | 52 065 | `25e13bf5c3fbfebe…` |
| `cycle_smoke_plan.json` | 16 | 8 | 1 132 | `848e47fdb6754343…` |

Il seed pesca soltanto **quali** cinquanta utenti si fermano, in quale ordine
rientrano, e quale username cerca ogni richiesta. Tutto il resto è aritmetica.
Generato due volte in directory diverse, il piano è identico byte per byte.

Nello smoke la fase si chiama ancora `pause_50` pur mettendo in pausa otto utenti:
il nome della fase è strutturale e non conta i suoi utenti.

## Lo smoke

```bash
./run_smoke.sh
```

Identico alla prova completa: otto core, quattro gibibyte, **otto processi per
stack**, un utente una richiesta al secondo, la stessa guardia di latenza con la
stessa soglia e le stesse quindici valutazioni, le due gambe in sequenza, le due
certificazioni.

Ridotto: sedici utenti invece di centoventi e **due per worker invece di
quindici** (`GNR_ASGI_WORKER_MAX_USERS=2`, così i sedici occupano gli stessi otto worker),
otto in pausa invece di cinquanta, finestre e osservazione accorciate.

Il carico ridotto non dice nulla sulla capacità: serve a dimostrare che gli
strumenti girano, che il journal si scrive, che il classificatore legacy nomina
gli otto worker, e che non resta nulla in piedi alla fine.

## La prova completa

```bash
./run_cycle.sh
```

Le due gambe in sequenza — legacy prima, bridge dopo — sullo stesso boot della
stessa macchina, con lo stesso piano. Una gamba che finisce male ferma la
sequenza. `./run_cycle.sh bridge,legacy` inverte l'ordine senza toccare una riga.

## Controlli locali

```bash
bash -n run_cycle.sh run_smoke.sh
python3 -m py_compile make_cycle_trace.py cycle_probe.py admission_guard.py
python3 tests/test_cycle_tools.py
```

Nessuno usa Docker. La sequenzialità delle gambe e l'arresto dello stack
precedente sono provati da `../bench_common/tests/test_lab_lifecycle.sh`, che
mette un `docker` finto davanti al `PATH` e registra ogni invocazione: è lo stesso
codice condiviso che questo runner usa senza modifiche. Il cablaggio degli
override di questo scenario si verifica invece con un render vero
(`docker compose config`), che mostra core, memoria, `GNR_ASGI_WORKER_MAX_USERS` e il path
del journal dentro il container.

## Cosa raccoglie, per fase e per stack

Popolazione autenticata, attiva e in pausa · offerte, avviate, completate,
pendenti e trattenute · richieste al secondo · p50/p95/p99 · lateness e deriva ·
l'evento `ADMISSION_STOP` · CPU totale, del commander, del template, dei worker
aggregata e per worker, del master Gunicorn, dei worker Gunicorn, del daemon · PSS
per ruolo · `memory.current` e `memory.peak` · numero di processi · distribuzione
degli utenti · errori applicativi e di trasporto · OOM, restart, fallback e
`process_quitted` dal journal · **la latenza della prima chiamata di ogni utente
che rientra** (`*_return_calls.json`).
