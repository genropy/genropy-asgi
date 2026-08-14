"""Elastic-pool v1.1 benchmark scenarios (S1-S6), driven over the load harness.

Each scenario prints the env the pool must be (re)started with, runs its actions while
a MonitorSampler writes a JSONL timeseries to the scratchpad, then emits BOTH the raw
observables (timeseries path + the relevant commander log lines) AND a heuristic
PASS/FAIL verdict — heuristic because a system that reacts over 5-35s (5s sensor, ~30s
smoothing) resists sharp assertions; the raw data is the authority, the verdict a hint.

The pool must already be running with the scenario's knobs (the script prints the exact
command). Start it from the genropy-asgi project root so `examples/multiworker_config.py`
resolves.

Usage (from temp/benchmark/assets, pool on 8081):
  PYTHONPATH=. python3 scenarios.py --scenario S1 --out <scratchpad>/S1.jsonl
"""

import argparse
import time

from load_harness import Fleet, MonitorSampler, settle, wait_pool_ready

BASE = "127.0.0.1:8081"


def reception_of(sampler):
    """The reception is the worker present since the first sample (the group's first
    routable worker). Return its name, or None if no worker yet."""
    row = sampler.sample()
    workers = row.get("workers", [])
    return workers[0] if workers else None


def occ_of(sampler, name):
    row = sampler.sample()
    pw = row.get("per_worker", {}).get(name, {})
    return pw.get("occ"), row.get("routable"), row.get("workers", [])


def warm_up(base):
    """Absorb the cold-start first GET / on the (single) fresh worker before load.

    A throwaway login warms the reception so the first real user doesn't eat the
    genro-asgi #46 cold-start 500. Uses the harness login (which itself re-GETs once).
    """
    f = Fleet(base)
    from load_harness import login_user
    c = login_user(f.host, f.port, f.login_calls, f.usernames[0], "a")
    return c


def scenario_s1(out_path, fast=False):
    """S1 — ramp-up / placement.

    Knobs: WORKERS=1 MAX_WORKERS=8 MIN_WORKERS=1 RECEPTION_THRESHOLD=0.5
           ADMISSION_THRESHOLD=0.8
    Login ~10 users at ~0.3/s, each with light steady traffic (~4 rps target). Expect:
    the first logins stay on the reception while its occupancy < 0.5; once it crosses,
    new logins land on an outer worker (least-occupied) and a scale-up fires.
    """
    print("=== S1 — ramp-up / placement ===")
    print("Pool must be started with:")
    print("  WORKERS=1 MAX_WORKERS=8 MIN_WORKERS=1 RECEPTION_THRESHOLD=0.5 "
          "ADMISSION_THRESHOLD=0.8 \\")
    print("  gnrasgiserve test_invoice_pg --config examples/multiworker_config.py "
          "-p 8081 --nodebug")
    print()

    wait_pool_ready(BASE)
    sampler = MonitorSampler(BASE, out_path, reception_threshold=0.5,
                             admission_threshold=0.8, compaction_margin=1.5, interval=2.0)
    reception = reception_of(sampler)
    print(f"reception worker: {reception}")

    print("warm-up (absorb cold-start on the fresh worker)...")
    warm = warm_up(BASE)

    sampler.start()
    fleet = Fleet(BASE)
    n_users, rate_r, rps = 10, 0.3, 4.0
    placement_log = []  # (user_index, worker_at_login, reception_occ_at_login)

    print(f"logging {n_users} users at {rate_r}/s, {rps} rps each ...")
    for i in range(1, n_users + 1):
        fleet.login_all(1, rate_r=rate_r, rps=rps)  # one more user
        time.sleep(3)  # let its traffic register a couple of samples
        row = sampler.sample()
        occ = row.get("per_worker", {}).get(reception, {}).get("occ")
        routable = row.get("routable")
        # where did the just-logged user land? read population for the newest user
        newest = fleet.users[-1].username
        placed = row.get("placement", {}).get(newest, {}).get("worker")
        placement_log.append((i, newest, placed, occ, routable))
        print(f"[{i:2d}] {newest:<20} -> worker={placed}  reception_occ={occ}  "
              f"routable={routable}")

    settle(35 if not fast else 12, "S1 tail", fast)
    final = sampler.sample()
    sampler.stop()
    fleet.stop_all()
    warm.conn.close()

    # --- raw observables ---
    print(f"\ntimeseries: {out_path}")
    print(f"final routable workers: {final.get('routable')}  "
          f"workers={final.get('workers')}")
    print("per-worker occupancy:", {n: w.get("occ")
                                     for n, w in final.get("per_worker", {}).items()})
    errs = fleet.errors()
    if errs:
        print("harness errors:", errs[:5])

    # --- heuristic verdict ---
    print("\n--- heuristic verdict (NOT authoritative; check the timeseries) ---")
    # PASS if: early logins all on reception; a second worker appeared; some login
    # landed off-reception only after reception_occ crossed ~50.
    off_recept = [(i, occ) for (i, _u, w, occ, _r) in placement_log
                  if w and w != reception]
    scaled = final.get("routable", 1) > 1
    early_on_reception = all(w == reception for (_i, _u, w, occ, _r) in placement_log
                             if occ is not None and occ < 45)
    verdict = "PASS" if (scaled and early_on_reception and off_recept) else "CHECK"
    print(f"reception kept early logins (<45 occ): {early_on_reception}")
    print(f"scaled to >1 worker: {scaled}")
    print(f"logins that landed off-reception: {off_recept}")
    print(f"VERDICT: {verdict}")


def scenario_s2(out_path, fast=False):
    """S2 — scale-up.

    Knobs: WORKERS=1 MAX_WORKERS=8 MIN_WORKERS=1 RECEPTION_THRESHOLD=0.5
           ADMISSION_THRESHOLD=0.8
    Log a handful of users, then raise per-user rps in steps until every outer worker
    sits over 0.8. Expect: workers spawn ONE AT A TIME (tracked +1 per step, never a
    simultaneous jump), each spawn preceded by the saturation WARNING / `scaled to K`;
    logins added during the climb prefer the newborn (occupancy 0 -> least-occupied).
    """
    print("=== S2 — scale-up ===")
    print("Pool must be started with:")
    print("  WORKERS=1 MAX_WORKERS=8 MIN_WORKERS=1 RECEPTION_THRESHOLD=0.5 "
          "ADMISSION_THRESHOLD=0.8 \\")
    print("  gnrasgiserve test_invoice_pg --config examples/multiworker_config.py "
          "-p 8081 --nodebug")
    print()

    wait_pool_ready(BASE)
    sampler = MonitorSampler(BASE, out_path, reception_threshold=0.5,
                             admission_threshold=0.8, compaction_margin=1.5, interval=2.0)
    print("warm-up (absorb cold-start)...")
    warm = warm_up(BASE)
    sampler.start()
    fleet = Fleet(BASE)

    # seed with 6 users at a modest rate, then climb rps in steps
    print("seeding 6 users @ 4 rps ...")
    fleet.login_all(6, rate_r=0.5, rps=4.0)
    settle(35 if not fast else 12, "seed", fast)

    tracked_series = []  # (step_label, tracked, routable, per-worker occ)

    def record(label):
        row = sampler.sample()
        occ = {n: w.get("occ") for n, w in row.get("per_worker", {}).items()}
        tracked_series.append((label, row.get("tracked"), row.get("routable"), occ))
        print(f"  [{label}] tracked={row.get('tracked')} routable={row.get('routable')} "
              f"occ={occ}")

    record("seed")
    for step, rps in enumerate([6.0, 8.0, 10.0, 12.0], 1):
        print(f"step {step}: raise all users to {rps} rps")
        fleet.set_rps(fleet.users, rps)
        # add one login mid-climb to see it prefer the newborn
        if step in (2, 3) and len(fleet.users) < len(fleet.usernames):
            fleet.login_all(1, rate_r=1.0, rps=rps)
            newest = fleet.users[-1].username
        else:
            newest = None
        settle(35 if not fast else 12, f"step{step}", fast)
        record(f"step{step}@{rps}rps")
        if newest:
            row = sampler.sample()
            placed = row.get("placement", {}).get(newest, {}).get("worker")
            print(f"    mid-climb login {newest} -> {placed}")

    final = sampler.sample()
    sampler.stop()
    fleet.stop_all()
    warm.conn.close()

    print(f"\ntimeseries: {out_path}")
    print(f"final routable={final.get('routable')} workers={final.get('workers')}")
    print("per-worker occupancy:", {n: w.get("occ")
                                    for n, w in final.get("per_worker", {}).items()})
    errs = fleet.errors()
    if errs:
        print("harness errors:", errs[:5])

    print("\n--- heuristic verdict (NOT authoritative; check the timeseries + log) ---")
    tracks = [t for (_l, t, _r, _o) in tracked_series if t is not None]
    max_jump = max((b - a for a, b in zip(tracks, tracks[1:])), default=0)
    grew = tracks and tracks[-1] > tracks[0]
    one_at_a_time = max_jump <= 1
    print(f"tracked series: {tracks}")
    print(f"grew under load: {grew}   max single-step jump: {max_jump} "
          f"(<=1 => one at a time)")
    verdict = "PASS" if (grew and one_at_a_time) else "CHECK"
    print(f"VERDICT: {verdict}")
    print("NOTE: confirm each spawn in the pool log: 'scaled to N' / "
          "'over admission threshold'")


def bench_ceiling(out_path, fast=False):
    """Throughput ceiling of the elastic multi-worker pool.

    The 2026-06 study found a single process tops out at ~60 req/s (GIL-bound on
    GenroPy's CPU render) and gunicorn 4-worker at ~166. This re-runs that "find the
    ceiling" test on the commander/worker elastic pool: distinct sticky users at
    saturating rps, concurrency raised in steps; the pool scales itself. Records, per
    step, the AGGREGATE throughput (sum of the commander's per-worker forward.requests
    deltas over the interval), worker count, and client-side latency p50/p90. The
    ceiling is the req/s beyond which adding load only raises latency.

    Uses usernames_all.txt (332 distinct identities) so concurrency can go high.
    """
    print("=== BENCH — throughput ceiling of the multi-worker pool ===")
    print("Pool must be started with:")
    print("  WORKERS=1 MAX_WORKERS=8 MIN_WORKERS=1 RECEPTION_THRESHOLD=0.5 "
          "ADMISSION_THRESHOLD=0.8 \\")
    print("  gnrasgiserve test_invoice_pg --config examples/multiworker_config.py "
          "-p 8081 --nodebug")
    print()

    wait_pool_ready(BASE)
    sampler = MonitorSampler(BASE, out_path, reception_threshold=0.5,
                             admission_threshold=0.8, compaction_margin=1.5, interval=2.0)
    print("warm-up (absorb cold-start)...")
    warm = warm_up(BASE)
    sampler.start()
    fleet = Fleet(BASE, usernames_file="usernames_all.txt")

    def forward_total(row):
        return sum((w.get("forward", {}) or {}).get("requests", 0)
                   for w in row.get("per_worker", {}).values())

    steps = [10, 30, 60, 100, 150, 200]
    rps = 8.0  # per-user target; the worker caps it, the queue absorbs the rest
    logged = 0
    results = []
    for target_c in steps:
        add = target_c - logged
        print(f"\n--- concurrency {target_c} ({add} new users @ {rps} rps) ---")
        fleet.login_all(add, rate_r=8.0, rps=rps)  # fast login ramp
        logged = target_c
        fleet.set_rps(fleet.users, rps)
        settle(35 if not fast else 12, f"c{target_c}", fast)
        # measure aggregate throughput over a 10s window
        r0 = sampler.sample()
        t0 = time.time()
        time.sleep(10)
        r1 = sampler.sample()
        dt = time.time() - t0
        agg_rps = (forward_total(r1) - forward_total(r0)) / dt
        workers = r1.get("routable")
        occ = {n: w.get("occ") for n, w in r1.get("per_worker", {}).items()}
        # client-side latency from the harness timings (getSelection only)
        lat = sorted(t[1] for u in fleet.users for t in u.timings if t[3] > 1000)
        p50 = lat[len(lat) // 2] * 1000 if lat else 0
        p90 = lat[int(len(lat) * 0.9)] * 1000 if lat else 0
        errs = len(fleet.errors())
        results.append((target_c, agg_rps, workers, p50, p90, errs))
        print(f"  aggregate: {agg_rps:.1f} req/s   workers={workers}   "
              f"p50={p50:.0f}ms p90={p90:.0f}ms   errors={errs}")
        print(f"  occ: {occ}")

    sampler.stop()
    fleet.stop_all()
    warm.conn.close()

    print(f"\ntimeseries: {out_path}")
    print("\n=== CEILING CURVE ===")
    print(f"{'concurrency':>11} {'req/s':>8} {'workers':>8} {'p50 ms':>8} "
          f"{'p90 ms':>8} {'errors':>7}")
    for c, r, w, p50, p90, e in results:
        print(f"{c:>11} {r:>8.1f} {w:>8} {p50:>8.0f} {p90:>8.0f} {e:>7}")
    peak = max(results, key=lambda x: x[1]) if results else None
    if peak:
        print(f"\npeak aggregate throughput: {peak[1]:.1f} req/s at concurrency "
              f"{peak[0]} with {peak[2]} workers")
        print("(ceiling = where req/s plateaus while p90 keeps climbing)")


SCENARIOS = {"S1": scenario_s1, "S2": scenario_s2, "BENCH": bench_ceiling}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", required=True, choices=sorted(SCENARIOS))
    ap.add_argument("--out", required=True, help="timeseries JSONL output path")
    ap.add_argument("--fast", action="store_true", help="shorter settles (unreliable)")
    args = ap.parse_args()
    SCENARIOS[args.scenario](args.out, fast=args.fast)


if __name__ == "__main__":
    main()
