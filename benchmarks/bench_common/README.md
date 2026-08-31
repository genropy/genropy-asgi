# bench_common — cosa i due scenari condividono davvero

Cinque moduli Python e due script shell. Niente che esista solo per sembrare
generale: ognuno di questi pezzi è usato da entrambi gli scenari, e se lo fosse
da uno solo starebbe dentro quello scenario.

| file | cosa fa |
|---|---|
| `stop_guard.py` | la bandiera di stop che ogni fase legge, e il guardiano della memoria che la alza |
| `container_probe.py` | una lettura del kernel per campione, con una mappa dei ruoli per stack |
| `bridge_eyes.py` | le letture del bridge che non lo toccano: census, status, deposito congelato |
| `load_engine.py` | il generatore a ritmo globale, identico per bridge e legacy |
| `page_class_cache.py` | il certificato della page-class cache di GenroPy, letto da fuori |
| `lab_lifecycle.sh` | la metà del contratto che sta nei runner: avvio, terminazione, cleanup, sequenza |
| `make_plans.sh` | genera i piani di uno scenario **una volta per campagna** e li certifica contro `plans.spec.json` |

## I piani si generano una volta, non una per stack

`make_plans.sh <scenario>` legge `plans.spec.json` — seed, argomenti esatti, hash
atteso — genera ogni piano mancante e confronta l'hash. Un piano che esiste già
viene **lasciato stare**: rigenerarlo per la seconda gamba sarebbe l'unico modo di
far leggere due file diversi alle due gambe. Un hash diverso da quello dichiarato
ferma tutto, perché vuol dire che il generatore o una sua dipendenza è cambiata.

Il ciclo di vita non genera piani: `lab_plan_digest` prende il digest una volta e
`lab_require_same_plan` lo ricontrolla prima di ogni gamba.

## Il certificato della page-class cache

Due verdetti tenuti separati — **abilitata** e **entry presenti** — e nessun hit
simulato, perché GenroPy non tiene alcun contatore di hit da leggere. Sul bridge
le tre letture passano dall'`eval` della console, nel processo worker; sul legacy
il valore configurato viene da un processo di servizio e le entry dalla pagina
`sys/page_class_cache` che GenroPy già porta.

Nessuna delle espressioni valutate muta lo stato: `page_class_cache_enabled()`
svuoterebbe la cache alla scadenza del TTL, e per questo non viene usata.

Sono strumenti di misura. Nessuno di questi file è importato da `src/`, e nessuno
appartiene a un pacchetto distribuito: esistono per confrontare il ponte con il
daemon legacy, e vivono sotto `benchmarks/` per quella sola ragione.

## Una bandiera, tre scrittori

Tre cose possono chiedere uno stop, e tutte e tre alzano la **stessa** bandiera,
così il driver ha una condizione da leggere e un solo percorso di chiusura:
l'operatore con TERM o INT, il guardiano della memoria, il driver stesso quando
un criterio strutturale cade.

`StopFlag.wait(secondi, dove)` dorme a piccoli passi: i riposi lunghi della prova
di popolazione durano minuti, e un `time.sleep(600)` ignorerebbe un TERM per
dieci minuti.

## Tre difetti della campagna precedente, chiusi per costruzione

| difetto | come è chiuso |
|---|---|
| `MEMORY_STOP` fermava solo i nuovi ingressi | la bandiera è letta da **ogni** fase: popolamento, lavoro, rotazione, riposo, logout |
| il driver era PID 1 e ignorava TERM | il driver gira sull'host come figlio del runner, quindi non e' PID 1; `install_signal_handlers` registra TERM e INT sulla stessa bandiera, e il runner manda TERM e **attende**, mai un kill. E' l'equivalente esplicito e verificabile di `init: true`, ed e' provato su un processo vero nei test |
| `orders.log` veniva rinominato mentre il bridge lo teneva aperto | il path si decide **prima** che il container nasca, via `GNR_ASGI_ORCH_LOG` col prefisso della corsa: il file nasce col nome finale e nessun `mv` esiste nel ciclo di vita |

Il guardiano, in più, **non smette di osservare** quando chiede lo stop: quello
che la memoria fa durante lo spegnimento è parte della prova. E il controllo
finale confronta sempre `memory.events` con la baseline, comunque la corsa sia
finita.

## Il guardiano legge dall'host

Un `docker inspect` all'inizio per il pid del container, poi
`/proc/<pid>/root/sys/fs/cgroup`. Dentro il container non gira niente: un
`docker exec` per campione aggiungerebbe un processo al container di cui si sta
misurando la memoria, quattro volte al secondo.

L'identità è il pid **più** il suo istante di avvio — campo 22 di
`/proc/<pid>/stat`, contato dall'ultima `)` perché un nome di processo con spazi
e parentesi sposterebbe il conteggio. Un pid riciclato non passa per lo stesso
processo.

`proc_dir` e `cgroup_dir` sono parametri: è ciò che rende il lettore
esercitabile offline contro un `/proc` fabbricato.

**Una lettura che non è un puro conteggio di byte non diventa mai uno zero.** Uno
zero è un fatto sulla memoria; `Unreadable` è un fatto sullo strumento.

## La mappa dei ruoli

La misura è la stessa per i due stack; cambia solo **come si chiamano** i
processi. Il bridge ha commander, template e i worker del pool, i cui nomi
arrivano dal census perché l'albero dei processi non può dire quale figlio sia
`pool_0003`. Il legacy ha il daemon, il master di Gunicorn e i suoi worker,
distinti per parentela e non per titolo: Gunicorn riscrive il titolo solo con
`setproctitle`, che l'immagine del laboratorio non installa.

Il master è il processo Gunicorn **che non ha un padre Gunicorn**. La parentela
verso l'init non basta a nominarlo: l'entrypoint del laboratorio fa `exec`, così
il master *è* il pid 1 e i suoi worker portano `ppid=1`. Dietro un init il master
è invece figlio del pid 1. La regola precedente — `gnrserveprod` con `ppid == 1`
— nominava i quattro worker e lasciava il master fra i non classificati.

Per questo esiste `certify`: il runner dichiara la forma che si aspetta e la
corsa si ferma se la classificazione non la produce.

## Controlli locali

```
python3 tests/test_stop_guard.py
python3 tests/test_container_probe.py
python3 tests/test_page_class_cache.py
bash tests/test_lab_lifecycle.sh
```

Nessuno usa Docker: il primo fabbrica un `/proc`, il secondo mette un
`docker` finto davanti al `PATH` e ne registra ogni invocazione, così "nessun
comando durante il cleanup" è un numero misurato e non un'affermazione.
