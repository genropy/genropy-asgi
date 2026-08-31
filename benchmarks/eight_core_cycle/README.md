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
| 2 | `full_warmup` | 60 s | 120/s | tutti attivi, **fuori dalla misura**: scalda ogni processo |
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

**Tutte le fasi sono obbligatorie.** Il driver le gioca tutte e sei, e
`require_every_phase` fa fallire l'esecuzione se una manca o se la popolazione non
si è riempita: una corsa che riporta un sottoinsieme *sembra* un risultato e non
lo è, perché i due stack verrebbero confrontati su lavori diversi. Una popolazione
incompleta fallisce prima, al login.

`full_warmup` sta fra la rampa e la prima misura, a popolazione piena e **fuori da
ogni finestra misurata**: il primo caricamento del sito costa secondi a ogni
processo di servizio, e quel costo non deve stare dentro una misura. Le sue
richieste, i suoi errori, la sua CPU e la sua memoria restano registrati.
Sostituisce `full_stabilize`, che erano gli stessi sessanta secondi di traffico
pieno sotto un nome che non diceva a cosa servissero.

## Il login si ritenta, e i 500 a freddo sono contati a parte

La prova misura il regime operativo, non il primo caricamento del sito. Un
processo di servizio a cui si chiede di costruire il sito **mentre risponde a un
login** risponde 500: succede sul legacy, dove Gunicorn consegna la connessione a
un worker ancora freddo, e non sul bridge, dove il placement attende che il worker
sia pronto.

Politica identica sui due stack:

| voce | valore |
|---|---|
| tentativi per login | **3** al massimo |
| connessione | **nuova a ogni tentativo** |
| attesa fra i tentativi | 2 s |
| se non entra dopo tre | l'esecuzione **fallisce** |

Ogni tentativo è registrato in `*_login_attempts.json` con timestamp, utente,
numero del tentativo, status HTTP, corpo della risposta, eccezione ed esito. I
tentativi falliti **prima** di un successo sono riclassificati `cold_start`:
contati nel loro contatore, riportati, e **mai** sommati agli errori di una
finestra misurata, che precedono. Nessun `GET /` anonimo, nessun guest, nessun
account o permesso toccato, nessun retry illimitato.

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

**Un secondo, un bucket, e i bucket non si sovrappongono.** Ogni chiamata
completata è archiviata sotto il secondo intero in cui è finita. Un bucket è
giudicato una volta sola, quando non può più ricevere chiamate, e poi buttato.

| voce | valore |
|---|---|
| grandezza | p95 delle chiamate completate in **un secondo intero** |
| soglia | **strettamente** maggiore di 1500 ms |
| quanti servono | **15 bucket cattivi consecutivi** |
| un bucket dentro soglia | **azzera** la sequenza |
| un bucket con meno di 5 campioni | **azzera** la sequenza |
| un secondo senza traffico | è un bucket magro: **azzera** |

Quindici bucket cattivi consecutivi sono quindi **quindici secondi consecutivi di
lentezza**, né più né meno. Cinque secondi non fermano la crescita; quattordici
non la fermano; al quindicesimo la porta si chiude.

Sostituisce una finestra mobile di dieci secondi, che era sbagliata per questa
decisione e in modo misurabile: un secondo lento restava dentro la finestra per
dieci valutazioni, quindi **circa cinque** secondi di lentezza reale producevano
già quindici violazioni consecutive. "Quindici valutazioni" e "quindici secondi"
non erano la stessa cosa. Con i bucket non sovrapposti lo sono.

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
  primo istante, quindi i due stack condividono una linea del tempo senza attese
  di pareggio.
- **`worker_min_life_seconds=3600`**: controllo sperimentale, non un valore di
  produzione. Il retirement non deve poter chiudere un worker a metà ciclo.
- **freeze assente, cioè mai**. La pausa di sessanta secondi deve lasciare i
  cinquanta residenti sul loro worker: un freeze li porterebbe su disco e la pausa
  misurerebbe il congelatore invece della residenza. Il driver certifica che
  `user_idle_freeze_minutes` torni `null` prima di misurare qualsiasi cosa, e
  ferma l'esecuzione se compare un solo utente congelato.

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
| `tests/test_cycle_tools.py` | i test mirati: piano, guardia, verdetto, mappa, ruoli |
| `tests/test_cycle_runner.sh` | il verdetto del runner, con un `docker` finto |
| `dynamic_recipe.py` | la recipe del bridge con la policy REALE, il pool decide |
| `run_profiles.sh` | i tre profili in sequenza, con certificazione viva |
| `tests/test_profiles.sh` | l'ordine dei profili, con un `docker` finto |

`CycleEngine` aggiunge quattro cose al motore condiviso e non ne toglie nessuna:
la riga si offre solo a un utente ammesso; ogni chiamata completata va alla
guardia; la **prima** chiamata di un utente che rientra è cronometrata a parte,
perché il costo del ritorno non si vede in una media; e tiene la mappa
etichetta → username.

Quella mappa serve alla colonna `worker` delle chiamate. Il census indicizza
`user_worker_map` sul **vero username di GenroPy**, mentre il motore conosce gli
utenti per l'etichetta `user_N`: senza la traduzione la ricerca non trovava mai
nulla e la colonna restava sempre vuota — in questo scenario e in quelli
precedenti. Un utente che il census non colloca resta assente dalla mappa: la
casella vuota è una **mancata osservazione**, non un worker inventato.

## Le due topologie, e i tre profili aggiunti

La prova a otto worker fissi è un **punto controllato**: crescita per CPU spenta,
quindici utenti per worker, esattamente otto worker. Resta valida per quello che è,
ma non è l'orchestrazione con cui il pool gira in produzione.

| topologia | la forma dei worker | che cosa il driver fa |
|---|---|---|
| `fixed` | **dichiarata** in anticipo | la **asserisce**: `[15×8]` o l'esecuzione fallisce |
| `dynamic` | è un **risultato** | la **registra**, e asserisce solo la forma delle decisioni |

Asserire un numero in topologia dinamica deciderebbe in anticipo la risposta alla
domanda per cui la prova esiste.

### I cinque profili della comparazione a otto core

| profilo | processi di servizio | recipe / riga | dati |
|---|---|---|---|
| `bridge_w8` | 8 worker fissi, 15 utenti ciascuno | `cycle_recipe.py` | corsa `e8cv5`, **non si ripete** |
| `bridge_dynamic` | **il pool decide** | `dynamic_recipe.py` | da misurare |
| `legacy_w8` | Gunicorn `-w 8` | — | corsa `e8cv5`, **non si ripete** |
| `legacy_w12` | Gunicorn `-w 12` | — | da misurare |
| `legacy_w16` | Gunicorn `-w 16` | — | da misurare |

Tutti a otto core e quattro gibibyte, sullo **stesso piano byte per byte**, con la
stessa politica di login, la stessa `full_warmup` e le stesse due guardie. Cambia
una cosa per volta.

```bash
./run_profiles.sh                      # legacy_w12 -> bridge_dynamic -> legacy_w16
```

Il runner **rifiuta di partire** se il piano non è quello della corsa valida: il suo
hash è dichiarato dentro il runner, e un workload diverso non si confronta con
quella corsa.

Prima di ogni profilo certifica la configurazione **viva**, letta dal container, e
blocca: `cpu.max` uguale a otto core, `memory.max` uguale a quattro gibibyte,
contatori OOM a zero, il numero esatto di processi Gunicorn con il suo master e il
suo daemon, il path del journal che il pid 1 porta davvero, e — sul profilo
dinamico — l'**assenza** del cap di utenti per worker.

### Che cosa resta bloccante in topologia dinamica

Il numero dei worker non è giudicato. È giudicata la **forma delle decisioni**,
letta dal journal dell'orchestratore e non indovinata:

- almeno un worker è nato;
- **ogni nascita oltre la prima porta la ragione "serviva a collocare un utente"**.
  Il core avvia un worker con il gruppo, quindi l'invariante è
  `start_worker == new_worker_created_for_placement + 1`. Se una scansione CPU
  avesse creato un worker da sé, le nascite supererebbero le ragioni di placement e
  l'uguaglianza cadrebbe — che è esattamente ciò che non deve accadere;
- nessuno fra `restart_worker`, `close_worker`, `process_quitted`,
  `placement_fallback` compare;
- nessun guest, nessun congelato, ogni utente autenticato effettivamente collocato,
  almeno un worker vivo a popolazione piena.

La cronologia dei worker (`*_worker_history.json`) porta una riga ogni volta che
l'insieme dei worker vivi cambia: quali sono nati, quali usciti, sotto quale padre,
in quale fase, con quanti utenti collocati, e CPU e memoria del container a quel
momento.

### Il tetto che ho dovuto scrivere

`worker_max_number` **non** si può lasciare al default: il core dice **sei**, meno
degli otto già misurati, quindi lasciarlo starebbe limitando la crescita sotto la
forma nota. È fissato a **sedici**, il doppio degli otto, e la corsa riporta il
massimo raggiunto: un risultato che tocca sedici è un risultato **limitato dal
tetto** e va letto come tale.

Ha un secondo effetto, dichiarato perché non è ovvio: il core deriva
`worker_memory_max_percent` come `100 / worker_max_number`. A sedici sono 6,25% di
quattro gibibyte, circa 256 MB per worker, contro i 50–60 MB che un worker teneva
davvero. Alzare il tetto restringerebbe quella quota.

## I worker attesi: due regole, non una

Il numero di processi di servizio che uno stack deve mostrare non si calcola allo
stesso modo sui due, e usare una condizione sola era sbagliato:

- il **legacy** non ha un pool. Gunicorn forka i suoi worker all'avvio e li tiene
  qualunque cosa faccia la popolazione, quindi il numero atteso è **sempre** quello
  dichiarato — anche a popolazione zero.
- il **bridge** fa crescere il pool solo dalla domanda di placement. Il numero
  atteso si deriva dagli utenti effettivamente **collocati**, a `worker_max_users`
  ciascuno, mai meno dell'unico worker che il gruppo avvia da sé, mai più del
  massimo configurato.

Le colonne del CSV di questo scenario sono quelle condivise più tre —
`users_active`, `users_paused`, `admission_stop` — e il formato dei CSV degli
scenari precedenti non è toccato: un test lo verifica.

## I piani

Non sono versionati: il piano pieno pesa 6,6 MB. Si rigenerano identici da
`plans.spec.json`, che porta il seed, gli argomenti esatti e l'hash atteso.

```bash
../bench_common/make_plans.sh .
```

| piano | utenti | in pausa | riscaldamento | richieste | sha256 |
|---|---|---|---|---|---|
| `cycle_plan.json` | 120 | 50 | 60 s | 52 185 | `376ea43df7141f77…` |
| `cycle_smoke_plan.json` | 16 | 8 | 15 s | 1 228 | `283eb1e12dfbd89e…` |

Il seed pesca soltanto **quali** cinquanta utenti si fermano, in quale ordine
rientrano, e quale username cerca ogni richiesta. Tutto il resto è aritmetica.
Generato due volte in directory diverse, il piano è identico byte per byte, e lo
è anche fra macOS e Linux: gli hash qui sopra sono gli stessi sulle due
piattaforme.

Cambiando il generatore, i piani già in `traces/` vanno rigenerati con `--force`:
`make_plans.sh` lascia stare un piano che esiste e ne verifica solo l'hash, per
non far leggere due file diversi ai due stack. Un hash diverso da quello
dichiarato ferma la campagna — ed è così che è stato colto un piano vecchio
rimasto in `traces/` dopo una modifica al generatore.

Nello smoke la fase si chiama ancora `pause_50` pur mettendo in pausa otto utenti:
il nome della fase è strutturale e non conta i suoi utenti.

## Lo smoke

```bash
./run_smoke.sh
```

Identico alla prova completa: otto core, quattro gibibyte, **otto processi per
stack**, un utente una richiesta al secondo, la stessa guardia di latenza con la
stessa soglia e le stesse quindici valutazioni, i due stack in sequenza, le due
certificazioni.

Ridotto: sedici utenti invece di centoventi e **due per worker invece di
quindici** (`GNR_ASGI_WORKER_MAX_USERS=2`, così i sedici occupano gli stessi otto
worker), otto in pausa invece di cinquanta, riscaldamento di quindici secondi
invece di sessanta, finestre e osservazione accorciate.

**Lo smoke esce non-zero se non esercita tutte le fasi.** È PASS solo se, su
entrambi gli stack: tutti gli utenti previsti entrano, `reached_full` è vero, tutte
e sei le fasi vengono eseguite, pausa e rientro avvengono, nessun `MEMORY_STOP`,
nessun OOM, topologia corretta, output completi e sigillati. Se una popolazione
incompleta facesse saltare le fasi, il driver scrive il motivo, esce non-zero, e
`lab_run_legs` ferma la sequenza prima che l'altro stack parta — con cleanup e
manifest eseguiti comunque.

Il carico ridotto non dice nulla sulla capacità: serve a dimostrare che gli
strumenti girano, che il journal si scrive, che il classificatore legacy nomina
gli otto worker, e che non resta nulla in piedi alla fine.

## La prova completa

```bash
./run_cycle.sh
```

I due stack in sequenza — legacy prima, bridge dopo — sullo stesso boot della
stessa macchina, con lo stesso piano. Un'esecuzione che finisce male ferma la
sequenza. `./run_cycle.sh bridge,legacy` inverte l'ordine senza toccare una riga.

## Controlli locali

```bash
bash -n run_cycle.sh run_smoke.sh
python3 -m py_compile make_cycle_trace.py cycle_probe.py admission_guard.py
python3 tests/test_cycle_tools.py
bash tests/test_cycle_runner.sh
bash tests/test_profiles.sh
```

Nessuno dei due avvia un container. `test_cycle_runner.sh` mette un `docker`
finto davanti al `PATH`, sostituisce il driver con uno che esce col codice che
vuoi, e legge dal registro del finto che cosa è stato invocato: così "il secondo
stack non è partito dopo il primo fallito" e "il cleanup è girato comunque" sono
fatti letti, non affermazioni. **Gira solo su Linux**, perché il runner esige
`/proc/loadavg` prima di toccare il laboratorio; su macOS si dichiara saltato.

Il cablaggio degli override si verifica con un render vero
(`docker compose config`), che mostra core, memoria,
`GNR_ASGI_WORKER_MAX_USERS` e il path del journal dentro il container.

## Cosa raccoglie, per fase e per stack

Popolazione autenticata, attiva e in pausa · offerte, avviate, completate,
pendenti e trattenute · richieste al secondo · p50/p95/p99 · lateness e deriva ·
l'evento `ADMISSION_STOP` · CPU totale, del commander, del template, dei worker
aggregata e per worker, del master Gunicorn, dei worker Gunicorn, del daemon · PSS
per ruolo · `memory.current` e `memory.peak` · numero di processi · distribuzione
degli utenti · errori applicativi e di trasporto · OOM, restart, fallback e
`process_quitted` dal journal · **la latenza della prima chiamata di ogni utente
che rientra** (`*_return_calls.json`) · **ogni tentativo di login con il suo
status, il suo corpo e il suo esito** (`*_login_attempts.json`) e il conteggio
separato degli errori `cold_start`.
