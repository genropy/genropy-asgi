# Verify — checks left to the human

## Phase 8

- **deferred: needs a session of the owner's own.** Start the twin proxy and
  browse through it: open a grid, save a record, and exercise table
  subscriptions and datachanges with two browser windows logged in as two
  different users (two profiles or one incognito — one profile's windows share
  the site cookie and are one user to both stacks). Then read the run's
  divergences and judge whether "no divergence left unexplained" is true.

  This is the roadmap border of macro-phase 2, and the only criterion of Phase 8
  left unmet: the phase closed short with the owner's ok on 2026-08-26, having
  been driven by `drive_login` with up to four users instead.

  ```bash
  GENRO_GNRFOLDER=$PWD/temp/gnr \
      PYTHONPATH=$HOME/Sviluppo/Genropy/genropy/worktrees/bench-baseline/gnrpy \
      GNR_DAEMON_PROVIDER=genropy-asgi PGGSSENCMODE=disable \
      python benchmarks/compare/twin_proxy.py test_invoice_pg_legacy \
      --run "<the name you give this run>" --max-users-per-worker 1
  ```

  Then browse http://127.0.0.1:8097. Clear the site cookie first, or the first
  RPC is refused as a connection from the run before (the `stale-connection`
  rule names it, and the run carries on).

  One WARNING is known and will appear: two `globalStore` calls from
  `getMainStorePreference`, one per browser, where the legacy makes the extra
  one. Anything else is new.
