# Confronto L120 — bridge W4 contro legacy Gunicorn

Le due gambe rispondono allo stesso piano di richieste, in sequenza, sullo stesso
boot della stessa macchina, a 120 richieste al secondo.

## Cosa lanciare

```
../bench_common/make_plans.sh .      # UNA VOLTA per campagna: genera e certifica i piani
./run_smoke.sh                      # finestre corte, popolazione vera: ~5 min per gamba
./run_compare.sh                    # il confronto, ordine legacy,bridge
./run_compare.sh bridge,legacy       # l'ordine inverso, senza cambiare una riga
./run_compare.sh legacy,bridge p2    # un altro prefisso per gli output
```

I runner girano su Linux: usano `sha256sum`, `bc` e `/proc/loadavg`.

## Le due configurazioni a confronto

| | bridge | legacy |
|---|---|---|
| processi | commander, template, 4 worker | gnrdaemon, master Gunicorn, 4 worker |
| utenti | 48, dodici per worker | 48 |
| come si ottiene | `worker_max_users=12` nella recipe | `LEGACY_WORKERS=4` |
| worker class | — | `gthread`, `--threads 16` (cablati nell'entrypoint) |
| CPU | 4 | 4 |
| memoria | `L120_MEM_LIMIT`, default 2g | lo stesso valore |
| policy | 50/30/80/0, quiet 60, restart 95, `worker_min_life_seconds=3600` | nessuna |

`worker_min_life_seconds=3600` è un **controllo sperimentale**, non una
configurazione di produzione: impedisce che il retirement chiuda un worker
durante la corsa e cambi da sé la topologia, che qui è fissa per disegno.

## Variabili d'ambiente

| variabile | chi la legge | default |
|---|---|---|
| `WORK_DIR` | il runner | `<scenario>/runs/<prefisso>` |
| `LAB_DIR` | il runner | `<scenario>/../docker` |
| `PLAN` | il runner | `traces/l120_plan.json` (generato, non versionato) |
| `L120_MEM_LIMIT` | i due override | `2g` |
| `LEGACY_WORKERS` | l'override legacy, obbligatoria | il runner la mette a 4 |
| `MEMORY_THRESHOLD` | il guardiano | `80` |
| `L120_DIR`, `L120_RUNTIME_DIR`, `GNR_ASGI_ORCH_LOG` | gli override | esportate dal runner |

## Il piano, e cosa significa "deterministico"

**I piani non sono versionati.** Pesano megabyte e si rigenerano identici da
`plans.spec.json`, che porta il seed, gli argomenti esatti e l'hash atteso di
ognuno. `traces/` è gitignored, e così `runs/`.

`../bench_common/make_plans.sh .` li genera **una volta per campagna** e confronta
ogni hash con quello dichiarato. Se un piano esiste già lo lascia stare e verifica
solo l'hash: rigenerarlo per la seconda gamba sarebbe l'unico modo di far leggere
due file diversi alle due gambe. `--force` rigenera, e serve solo quando cambia la
specifica. Un hash diverso ferma tutto: vuol dire che il generatore o una sua
dipendenza è cambiata, e i numeri delle campagne precedenti non sarebbero più
confrontabili.

**Come si garantisce che le due gambe leggano gli stessi byte**, in tre punti:

1. `run_compare.sh` prende il digest del piano **una volta**, prima della sequenza;
2. prima di **ogni** gamba lo ricontrolla (`lab_require_same_plan`): un piano
   toccato fra le due misure non passa inosservato;
3. il driver riceve quel digest con `--plan-sha256` e lo confronta con l'hash dei
   byte che ha **effettivamente letto**, nella stessa lettura che ha prodotto il
   piano. Il digest finisce in `_outcome.json` di ogni gamba.

Il piano porta il protocollo oltre alle richieste, così le due gambe non possono
divergere sulla forma della corsa.

Cosa viene dalla cattura e cosa no, perché la parola deterministico non copra
un'ambiguità:

- il **login** è quello di `session_capture.jsonl`, verbatim, replayato con
  l'identità di ogni account. Non sta nel piano perché non è un'estrazione: è un
  fatto della cattura, letto a tempo di corsa;
- l'**unità di carico** è la `app.getSelection` indicizzata su `adm.user` che
  tutte le corse di questa campagna hanno usato — una riga, una query indicizzata,
  una busta di circa 700 byte. Le `getSelection` pesanti della cattura **non**
  vengono rigiocate;
- le **estrazioni** sono quale utente emette ogni richiesta e quale username
  cerca. Sono seminate e materializzate nel file.

Rigiocare le `getSelection` della cattura sarebbe più fedele a un browser e meno
comparabile con tutto ciò che è stato misurato finora. La scelta è la seconda, ed
è del titolare cambiarla.

```
python3 make_trace.py --out traces/l120_plan.json --seed 20260831
```

Il piano pieno: 19200 richieste, 48 utenti, warmup 30s a 40/s, stabilizzazione
30s a 120/s, misura 120s a 120/s.

## Cosa misura

Per finestra: offerte, avviate, completate al secondo; p50, p95, p99; lateness di
partenza con la sua deriva; code pendenti; errori **separati** in HTTP,
applicativi e di trasporto.

Per campione, ogni due secondi: CPU e PSS **per ruolo**, `memory.current`,
`memory.peak`, `memory.events`, processi vivi, e — sul solo bridge — i quattro
conteggi della popolazione e la distribuzione per worker.

### Tre numeri, tre istanti

`offerte` è una riga del piano il cui istante è arrivato; `avviate` è una
richiesta consegnata al socket; `completate` è una risposta letta per intero. La
sensitivity riportava avviate e completate dalla stessa lista, quindi i due
numeri erano uguali per costruzione e una richiesta partita e mai tornata era
invisibile.

### La lateness non è un verdetto

Quando la lateness di partenza cresce, il generatore non è automaticamente
guasto: se il bridge smette di completare al ritmo dell'offerta, la coda è un
sintomo della saturazione dello stack. La finestra riporta i numeri e **non**
emette giudizi sul generatore: il giudizio richiede la CPU del generatore e
quella dell'host, e si dà nel rapporto.

## La certificazione della page-class cache

Fuori dalla finestra misurata, **subito dopo il warmup**, il driver scrive
`_page_class_cache.json`.

**Bloccante** è solo ciò che rende confrontabili i due stack. Se manca, la gamba
si ferma:

| fatto | come si certifica |
|---|---|
| `configuration_enabled` | la preferenza `sys.experimental.page_class_cache` vale davvero `True`. È una riga di `adm.preference`, letta da un processo di servizio nel container di **quello** stack: i due hanno database distinti, e leggere ciascuno il suo è il solo modo perché una divergenza si veda |
| `genropy_revision` | la revisione del tree GenroPy montato, letta sull'host dal path del `.env`. Le due gambe montano lo stesso path, e ogni certificato registra path e revisione |
| `requests_carry_page_id` | il carico porta un `page_id`. Una richiesta che non lo porta non entra mai in cache |
| `avoid_module_cache` | il carico **non** porta `_avoid_module_cache`, che bypasserebbe la cache richiesta per richiesta |

Gli ultimi due sono fatti della richiesta che il driver stesso costruisce: si
leggono sul form, senza interrogare nessun server.

**Diagnostiche**, e non fermano mai la prova: `entries_status`, `entries`,
`entries_source`, `entries_note`.

| stack | entry |
|---|---|
| bridge | `page_class_cache_entries()` per worker, dall'`eval` della console |
| legacy | solo se `sys/page_class_cache` è già accessibile. È gated `superadmin,_DEV_`: se l'account del banco non ha già il tag, `entries_status: unavailable` con la ragione, **e la corsa continua** |

Nessun tag viene assegnato, nessun account creato, nessun endpoint, middleware,
hook Gunicorn, wrapper o contatore aggiunto. `entries_status` esiste perché "non
osservabili" non si confonda mai con `entries = 0`. E nessun hit è simulato:
GenroPy non tiene contatori di hit, quindi un tasso di hit non è fra le cose che
questo certificato può affermare. Sul legacy, in più, una singola richiesta
parlerebbe di un worker Gunicorn su quattro, e il certificato lo dice.

Perché dopo il warmup e non altrove: una entry nasce solo per una richiesta che
**porta** un `page_id` e **muore** alla chiusura della pagina. Misurare dopo il
logout leggerebbe zero su una cache che ha funzionato.

Niente sta nel percorso misurato: il carico è fermo mentre la certificazione gira,
e sul bridge la lettura passa dalla console, che il demux devia sul primo segmento
prima del sito. La console si monta solo con `GNR_ASGI_CONSOLE`, che l'override
passa a 1.

## L'asimmetria voluta nell'attesa dell'avvio

Sul bridge si attende il **census**; sul legacy un **200 sulla radice**. Non è
una dimenticanza: sul bridge un `GET /` conia un guest, e un guest occupa uno
slot di `worker_max_users` falsando la distribuzione. Sul legacy quella
contabilità non esiste.

## Il journal nasce col nome finale

`GNR_ASGI_ORCH_LOG` viene deciso **prima** che il container esista e porta il
prefisso della corsa, così il log umano e il suo `.decisions.jsonl` nascono già
col nome definitivo. Il vecchio schema rinominava `orders.log` mentre il bridge
lo teneva aperto: il processo continuava a scrivere nell'inode rinominato e
`orders.log` non esisteva più.

## Output

Per gamba, sotto `WORK_DIR`, col prefisso `<prefisso>_<stack>`:

| file | contenuto |
|---|---|
| `_samples.csv` | un campione ogni due secondi, 46 colonne |
| `_calls.csv` | una riga per richiesta completata, coi tre istanti |
| `_windows.json` | i numeri di ogni finestra |
| `_checkpoints.json` | distribuzione e apply, con i loro problemi |
| `_population_log.json` | la popolazione dopo ogni login |
| `_logouts.json` | l'esito di ogni logout |
| `_outcome.json` | l'esito della gamba, il verdetto di memoria, le cause di stop |
| `_memory_guard.json` | i campioni del guardiano e il suo verdetto |
| `_journal_events.json` | decisioni e reason del journal (solo bridge) |
| `_compose.yaml` | il render dello scenario, come è stato eseguito |
| `_driver.log` | stdout e stderr del driver |

Più `run_compare_status.txt` con l'esito di ogni gamba e `MANIFEST.sha256`,
scritto **solo** a writer chiusi.

## Dipendenze esterne allo scenario

Restano dove sono, tracciate dal repository:

| file | a cosa serve |
|---|---|
| `benchmarks/churn_driver.py` | `LoggedUser`, `load_capture`, `build_plan` |
| `benchmarks/replay_a1.py` | importato da `churn_driver.py` |
| `benchmarks/single_record_bench.py` | il template `WHERE` del filtro |
| `benchmarks/session_capture.jsonl` | la cattura da cui nasce il login |
| `benchmarks/usernames_all.txt` | gli username che il filtro pesca |
| `benchmarks/bench_common/` | stop, guardiano, campionatore, motore di carico |
| `benchmarks/docker/` | il laboratorio su cui gli override si innestano |

`TOOLS.sha256` e `MANIFEST.sha256` coprono i file **di questo scenario**: le
dipendenze le garantisce git.

## Controlli locali, senza Docker

```
python3 tests/test_l120_tools.py
python3 ../bench_common/tests/test_stop_guard.py
bash ../bench_common/tests/test_lab_lifecycle.sh
```
