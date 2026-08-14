# Benchmarks

Load harness for the daemon-less bridge: a faithful replay of a real GenroPy
browser session, driven at increasing concurrency against a running instance.
Stdlib only — no locust, no k6, nothing to install.

Every script is run **from inside this directory** (they open their data files by
relative name and put `.` on the import path).

## The two things you need first

**A running instance.** The single process:

```bash
PGGSSENCMODE=disable gnrasgiserve test_invoice_pg -p 8099 --nodebug
```

`PGGSSENCMODE=disable` is required on macOS: libpq negotiating Kerberos in a
forked child segfaults the process.

**Accounts whose password you know.** The harness logs in as the 32 usernames
listed in `usernames.txt`, all sharing one password (`--password`, default `a`).
The accounts themselves come with the test instance; what `test_users.zip`
carries is the pair of fixtures used to make them usable for a benchmark:

- `set_pwd_a.sql` — 32 `UPDATE` statements, one per username, in a single
  transaction: this is the one you run, and it sets every password to `a`;
- `adm_user_backup_20260621_073407.sql` — the `pg_dump` of `adm.adm_user` taken
  immediately BEFORE that overwrite, kept so the original passwords can be
  restored. It recreates the table from scratch (no `DROP`, no `IF NOT EXISTS`),
  so restoring means dropping `adm.adm_user` first — never a plain replay onto a
  live table.

```bash
unzip -o test_users.zip
psql -d <your_test_db> -f set_pwd_a.sql
```

Fictional accounts on a local test database: invented names,
`@testinvoice.com` addresses, and the hash of the single letter `a`. Zipped
rather than left loose so the archive reads as the database fixture it is
instead of presenting itself as credential material. Never aim either file at
anything but a throwaway database.

## The trap that cost a full session — read this before writing a new script

GenroPy's login is two RPC calls, and they carry the identity **differently**:

- `login_checkAvatar` has flat form fields — `user=`, `password=`;
- `login_doLogin` carries them **inside an XML Bag** in the `login` field:
  `<GenRoBag><user>amelia.martin</user><password>a</password></GenRoBag>`.

A script that replays the captured form and only overwrites the flat fields logs
**every** session in as the captured user, however many distinct usernames it was
given. The whole run then measures one user, and anything that depends on user
identity — sticky routing, per-user placement across worker processes — silently
measures nothing.

Always build the login form with `inject_identity()`, which rewrites both places:

```python
from replay_a1 import inject_identity
f = inject_identity(captured_form, username, password)
```

Never add a key the captured form does not have: a `password=` field alongside
the `login` Bag makes `getAvatar()` fail with multiple values.

> There are currently **two** copies of this function — `replay_a1.inject_identity`
> and `scaling_probe.inject_identity` (imported from there by `load_harness`).
> They are equivalent. Consolidating them is an open cleanup.

## The measurement ladder

Each rung isolates one more layer, so a slowdown can be attributed instead of
guessed:

| Script | What it measures |
|---|---|
| `floor_bench.py` | The framework floor: no DB, no page logic. |
| `ping_ramp.py` | The authenticated `/_ping` as logged sessions climb — the polling cost. |
| `single_record_bench.py` | One indexed query, ~700-byte envelope: the per-RPC machinery. |
| `replay_a1.py` | The full replay: frame login, TH pages, the heavy `getSelection` calls. |

Typical run:

```bash
PGGSSENCMODE=disable python3 single_record_bench.py --levels 1,2,4,8 --duration 15
```

Every script takes `--base` (default `http://127.0.0.1:8099`), so the same rung
can be pointed at a classic gunicorn stack for comparison.

## Data files

| File | Used by |
|---|---|
| `session_capture.jsonl` | The recorded browser session every replay is built from (`capture_proxy.py` records it). No cookies or session tokens — request lines and form fields only. |
| `usernames.txt` / `usernames_all.txt` | The pool of accounts sessions log in as. |
| `cust_pkeys.txt` | Customer record keys for the record-level rungs. |
| `test_users.zip` | The two SQL fixtures: set every benchmark account's password, and the pre-overwrite dump that restores the originals (see above). |

## The rest of the scripts

Kept for their methodology and as baselines; several predate the current
architecture, so read the docstring before trusting a number.

| Script | What it was for |
|---|---|
| `capacity_bench.py`, `capacity_bench_record.py`, `capacity_bench_sticky.py` | How many realistic users one worker hosts — direct, light-DB, and through the sticky front. |
| `load_harness.py`, `scenarios.py`, `run_grid.py` | The elastic-pool scenario suite (S1–S6) and the threads × users grid. |
| `scaling_probe.py` | Distinct users logging in one after another, watching the pool grow. |
| `cost_model_ramp.py` | Base cost plus per-user marginal cost on one worker. |
| `capture_proxy.py`, `capture_loadrecord.py` | Record a live browser session; capture one repeatable `loadRecordCluster`. |
| `sr_counter.py`, `wire_counter.py`, `gil_ramp.py`, `gunicorn_*.conf.py` | Instrumentation from the register/daemon era: register calls per RPC, wire calls to the daemon, thread-load behaviour. `gil_ramp.py` and the gunicorn probes reference the removed daemon modules and no longer run as they are. |
