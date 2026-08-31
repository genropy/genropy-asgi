# Sensitivity sul numero di worker — strumenti

Le quattro corse W1, W2, W4, W8 a 48 utenti. L'unica leva che cambia fra loro e'
`GNR_ASGI_WORKER_MAX_USERS`; tutto il resto sta nella recipe `prep_config.py`,
uguale ovunque.

## Cosa lanciare

```
./run_smoke.sh              # sei utenti, due worker: prova che il laboratorio risponda
./run_all.sh                # le quattro corse in sequenza, stop alla prima che fallisce
./run_one.sh W2 2 24        # una corsa sola: nome, worker attesi, utenti per worker
```

Gli script funzionano da qualunque directory corrente.

## Variabili d'ambiente

| variabile | chi la legge | default |
|---|---|---|
| `WORK_DIR` | i tre runner | `<scenario>/runs` |
| `LAB_DIR` | `run_one.sh`, `run_smoke.sh` | `<scenario>/../docker` |
| `SENSITIVITY_DIR` | gli override, per montare `prep_config.py` | esportata dai runner: la directory dello scenario |
| `BENCHMARKS_DIR` | `worker_probe.py`, per le sue dipendenze | `<scenario>/..` |
| `SENSITIVITY_RUNTIME_DIR` | gli override, per montare `/lab/runtime` | esportata dai runner: `$LAB_DIR/runtime` |

## Cosa sono questi strumenti

Sono strumenti di misura del ponte GenroPy: misurano `genropy-asgi` che serve un
sito legacy sul pool di `genro-asgi`. Non sono test di `genro-asgi`, non
verificano il core in isolamento e non fanno parte di nessuna suite. Non
producono nulla dentro i sorgenti: campioni, log e reliquie di ogni corsa
finiscono in `WORK_DIR`, fuori da `src/` e fuori da questo scenario.

## Dipendenze esterne allo scenario

Lo scenario NON e' autosufficiente. Dipende da questi file del repository, che
restano dove sono e non sono copiati qui:

| file | a cosa serve |
|---|---|
| `benchmarks/churn_driver.py` | `worker_probe.py` ne importa `LoggedUser`, `load_capture`, `build_plan` |
| `benchmarks/replay_a1.py` | importato da `churn_driver.py` |
| `benchmarks/single_record_bench.py` | importato da `churn_driver.py` |
| `benchmarks/session_capture.jsonl` | la cattura da cui nasce il piano delle chiamate |
| `benchmarks/usernames_all.txt` | gli account con cui il driver popola i worker |
| `benchmarks/docker/compose.yaml` | il laboratorio su cui gli override si innestano |
| `benchmarks/docker/entrypoints/bridge.sh` | passa `--config $GNR_ASGI_POOL_RECIPE` a `gnrasgiserve` |

Nessuno di questi entra in `TOOLS.sha256` o in `MANIFEST.sha256`: i due manifest
coprono soltanto i file dello scenario. Le dipendenze sono tracciate dal
repository, e la loro integrita' la garantisce git.

I tre file che `worker_probe.py` apre — `churn_driver.py`, `session_capture.jsonl`,
`usernames_all.txt` — si risolvono dalla posizione di `worker_probe.py`, mai dalla
directory corrente. Se uno manca, il driver esce subito con l'elenco di quelli
assenti.

## Sorgenti importati

Gli override impostano
`PYTHONPATH=/src/genro-asgi/src:/src/genropy-asgi/src`, i due worktree montati
dal compose. Il processo del bridge importa da li', non da `site-packages`.

## I due manifest, e perche' i conti non tornano a occhio

- `TOOLS.sha256` elenca gli input: tutto tranne se stesso e `MANIFEST.sha256`.
- `MANIFEST.sha256` elenca gli input piu' `TOOLS.sha256`: tutto tranne se stesso.

Nessun manifest contiene la propria riga, perche' scriverla cambierebbe l'hash
che la riga dichiara. Da qui la differenza costante fra il numero di file nella
directory e il numero di righe di ciascun manifest.

## Piattaforma

I runner girano su Linux: usano `sha256sum`, `bc` e `/proc/loadavg`.
