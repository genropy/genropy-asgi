# Popolazione 2000, working set 80 — il freeze del bridge contro il legacy

Duemila utenti autenticati, ottanta al lavoro, millenovecentoventi in silenzio.
Il bridge congela gli inattivi; il legacy non ha quella capability e li tiene
tutti in memoria. Quello è il numero che la prova vuole leggere.

## Cosa lanciare

```
../bench_common/make_plans.sh .      # UNA VOLTA per campagna: genera e certifica i piani
./run_smoke.sh                      # 20 utenti, freeze a 1 min, 4 GiB: ~10 min
./run_pilot.sh                      # 200 utenti, 8 GiB, gate umano: ~36 min
./run_full.sh                       # 2000 utenti, 24 GiB, due gambe: ~3h45
```

**I limiti di memoria sono decisi**: 4 GiB per lo smoke, **8 GiB per il pilot**,
**24 GiB per la piena** — uguali per le due gambe, che girano sempre in sequenza e
mai insieme. `POP_MEM_LIMIT` li sovrascrive se serve. `run_population.sh`, se
invocato direttamente, continua a pretenderla: chi chiama il motore a mano deve
dichiarare il limite.

I runner girano su Linux: usano `sha256sum`, `bc` e `/proc/loadavg`.

## Il pilot è un gate umano

`run_full.sh` si rifiuta di partire se il pilot non ha lasciato i suoi output.
Il gate guarda che i tre file **esistano e non siano vuoti**; non ne giudica il
contenuto. Leggerli e decidere se la piena ha senso è del titolare — un controllo
automatico che lo facesse al suo posto sarebbe un GO implicito.

Il pilot produce dati, ripristina l'ambiente, termina. Non passa alla piena.

## Le otto fasi

| # | fase | cosa accade |
|---|---|---|
| 1 | `populate` | gli utenti entrano a gruppi, uno al secondo |
| 2 | `rest` | silenzio più lungo della finestra di freeze |
| 3 | `wake` | il working set torna, un utente alla volta |
| 4 | `work` | il working set lavora, con le pause del piano |
| 5 | `rotate` | pochi escono dal working set e altrettanti rientrano |
| 6 | `rest2` | silenzio di nuovo |
| 7 | `logout` | tutti escono |
| 8 | `observe` | silenzio, per vedere la memoria tornare |

Il risveglio è **uno alla volta e diluito**: una raffica di rientri misurerebbe
la coda del freezer invece di un singolo thaw.

## Il freeze, e la sua incertezza

Il valore arriva dalla recipe via `GNR_ASGI_IDLE_FREEZE_MINUTES`. Senza quella
variabile il core usa il proprio default, che è `math.inf`: **nessuno viene mai
congelato**, e una corsa che si aspettasse un freeze misurerebbe una popolazione
che è semplicemente rimasta. Per questo:

- l'override rende la variabile obbligatoria;
- il runner certifica che sia arrivata dentro il container leggendo
  `/proc/1/environ` — un cambio di variabile **non sopravvive** a `restart`,
  serve `up -d --force-recreate`;
- il driver certifica il valore **vivo** da `/_orchestration/status`, dove
  `user_idle_freeze_minutes` è `null` quando il freeze è spento;
- il runner **ripristina sempre** il valore precedente all'uscita, anche se la
  corsa muore a metà: la trap è armata prima che il freeze venga toccato.

**La granularità reale è di circa sessanta secondi.** Il vertice giudica
l'inattività ogni dodici battiti da cinque secondi, su una fotografia vecchia
fino a cinque. Un freeze dichiarato a cinque minuti avviene fra i cinque minuti e
i sei minuti e cinque secondi dall'ultima chiamata. Quelle cadenze sono costanti
di modulo del core, non configurazione: la banda non si può restringere, solo
superare. Per questo il generatore del piano **rifiuta** un riposo che non superi
la soglia più 65 secondi, e il driver rifiuta un piano che lo faccia.

## Gli inattivi sono davvero inattivi

Un utente fuori dal working set non ha thread, non ha timer e non manda niente.
Due fatti lo rendono necessario e non solo ordinato:

- il bridge lascia che un client sposti in avanti il proprio orologio di attività
  attraverso `/_ping` — il core difende `last_refresh_ts` ma non le due clock che
  il giudice del freeze legge — quindi un driver che pingasse deciderebbe da sé
  il proprio freeze;
- un `GET /` senza cookie fa coniare un guest al sito, quindi un polling anonimo
  gonfierebbe la popolazione a ogni giro.

Le sole letture del driver sono il census e lo status dell'orchestrazione, che il
demux del pool devia **prima** del sito: non timbrano nessun orologio e non
coniano nessun guest. Il controllo è nei test, sull'albero sintattico del driver.

## Le tre misure che il core non offre

| misura | perché non c'è, e come viene presa qui |
|---|---|
| il conteggio dei congelati | un freeze **riuscito** non scrive nulla nel journal: solo un freeze rifiutato viene loggato. Il conteggio viene dal census, chiave `user_map[<utente>]["frozen"]` |
| la latenza del thaw | nessun timer, da nessuna parte, e nessuna riga di journal. Il thaw è sincrono dentro la prima richiesta dell'utente che torna: si cronometra quella, e a parte l'intera raffica che segue |
| lo spazio dei dati congelati | `FreezeHandler` espone i nomi delle cartelle e lo spazio libero dell'intero filesystem, mai la stanza che il freezer occupa. Si misura con un `du` sul deposito |

I quattro conteggi della popolazione hanno una trappola chiusa nel codice: un
utente congelato è **anche** non collocato, perché il freeze gli mette il
piazzamento a `None` lasciandolo nella mappa. Qui `unplaced` significa "non
collocato e non congelato", altrimenti ogni congelato si conterebbe due volte.

## Il legacy

Nessun freeze, e non si simula: stessa popolazione, stesso working set, stessa
rotazione, stessi tempi — li decide il piano. Si misura la memoria di gnrdaemon,
del master Gunicorn e dei suoi worker. Le due gambe girano in sequenza, mai
insieme.

## Il piano

**I piani non sono versionati**: si rigenerano identici da `plans.spec.json`, che
porta seed, argomenti e hash atteso di ognuno. `traces/` e `runs/` sono
gitignored.

```
../bench_common/make_plans.sh .          # una volta per campagna
../bench_common/make_plans.sh . --force  # solo se cambia la specifica
```

Le due gambe leggono lo stesso file, e lo si dimostra in tre punti: il runner
prende il digest una volta, lo ricontrolla prima di ogni gamba, e il driver
confronta l'hash dei byte che ha effettivamente letto con quello ricevuto in
`--plan-sha256`, registrandolo in `_outcome.json`.

Il generatore materializza tutto ciò che è un'estrazione: chi entra quando, quanto dura ogni
pausa, e **quali** utenti escono dal working set e quali entrano, a quale
istante. La campagna precedente estraeva la rotazione a tempo di corsa, quindi le
due gambe ruotavano identità diverse.

Il piano verifica anche il file degli account prima di adottarlo: unicità,
formato, assenza di righe che sembrino contenere un segreto, e ne registra
l'hash. `accounts/load_users.txt` sono 2000 righe `loaduser0001`..`loaduser2000`,
distinte, senza password.

## Memoria — decisa

8 GiB per il pilot a 200 utenti, **24 GiB per la piena a 2000**, uguali per le due
gambe. Su una macchina da 61 GiB la piena lascia 37 GiB a host e PostgreSQL (che
nel compose ha 1 GiB).

Il vincolo è il **legacy**, non il bridge: duemila sessioni autenticate restano
tutte in memoria, e il costo per sessione legacy a questa scala non è ancora
misurato. Il valore è scelto largo, e si stringerà sui dati del pilot.

Il guardiano ferma la corsa all'80% del limite.

## Output

Per gamba, col prefisso `<prefisso>_<stack>`:

| file | contenuto |
|---|---|
| `_phases.csv` | una riga per confine di fase: i quattro conteggi, i worker coi loro PID, la memoria, la taglia del freezer |
| `_samples.csv` | un campione ogni cinque secondi |
| `_calls.csv` | una riga per chiamata, con `kind` che marca la prima dopo un thaw |
| `_thaw.json` | per ogni rientro: la prima chiamata e l'intera operazione |
| `_rotation.json` | gli scambi eseguiti, con l'esito di ogni uscita |
| `_logouts.json` | l'esito di ogni logout |
| `_outcome.json` | l'esito, il verdetto di memoria, la certificazione del freeze |
| `_memory_guard.json` | i campioni del guardiano e il suo verdetto |
| `_journal_events.json` | decisioni e reason del journal (solo bridge) |

## Il guardiano della memoria

Legge i gauge **dall'host**: un `docker inspect` all'inizio per il pid, poi
`/proc/<pid>/root/sys/fs/cgroup`. Dentro il container non gira niente — un
`docker exec` per campione aggiungerebbe un processo al container di cui si sta
misurando la memoria.

Ferma la corsa all'80% del limite (parametrico) o al primo incremento di `max`,
`oom`, `oom_kill`, `oom_group_kill`. **Dopo aver chiesto lo stop continua a
osservare** fino alla conclusione effettiva del driver: quello che la memoria fa
durante lo spegnimento è parte della prova. Il controllo finale confronta sempre i
contatori con la baseline, comunque la corsa sia finita.

Un memory stop è un **FAIL di sicurezza**, e non va confuso con una normale
saturazione prestazionale: il runner esce con 7.

Una lettura che non è un puro conteggio di byte non diventa mai uno zero: diventa
un errore dichiarato.

## Controlli locali, senza Docker

```
python3 tests/test_population_tools.py
python3 ../bench_common/tests/test_stop_guard.py
bash ../bench_common/tests/test_lab_lifecycle.sh
```
