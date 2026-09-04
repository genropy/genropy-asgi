"""Build the HTML report pages of a session_bench measurement from its CSV files.

Two page families, one per point of view:

* ``tour`` (type 1, the user's view) -- one panel per user: the median of HIS
  response times per 5-second interval of HIS OWN tour, against the
  single-user baseline of the same stack.  Under the curves, two context
  bands: the ingress requests per second in that user's window (gray bars,
  mean of the two stacks) and the mean occupancy of each genropy-asgi worker
  in the same interval (pink columns, right scale 0-70%).
* ``churn`` (type 2, the machine's view) -- five strips on the run clock:
  median of ALL calls, population, ingress req/s, worker occupancy,
  container memory.

Input is what ``session_bench.py`` writes -- ``<name>_calls.csv`` and
``<name>_seconds.csv`` for each stack -- plus the container samples of
``docker/scripts/sample_stats.sh`` for the memory strip.

Bucket rule (FIXED): 5-second buckets.  In a tour page the buckets start at
the user's OWN first call, so the same interval holds the same calls for
everyone; in a churn page they start at the run clock's origin.

The numbers are computed here and written into the page as JavaScript data;
the drawing (monotone cubic interpolation, bands, strips) lives in the page.
"""

import argparse
import csv
import json
import statistics


class CallLog:
    """One stack's per-call CSV: the response times, by user and by tour offset."""

    def __init__(self, path):
        self.path = path
        with open(path) as handle:
            self.rows = [row for row in csv.DictReader(handle) if row["kind"] == "tour"]

    @property
    def user_list(self):
        return sorted({int(row["user"]) for row in self.rows})

    @property
    def tour_sum_list(self):
        return [self.get_tour_sum(user) for user in self.user_list]

    def get_user_rows(self, user):
        return [row for row in self.rows if int(row["user"]) == user]

    def get_window_start(self, user):
        mine = self.get_user_rows(user)
        return min(float(row["wall"]) - float(row["offset_s"]) for row in mine)

    def get_tour_buckets(self, user, buckets):
        """Median response time per 5 s bucket of this user's own tour."""
        mine = self.get_user_rows(user)
        origin = min(float(row["offset_s"]) for row in mine)
        series = []
        for index in range(buckets):
            low = origin + index * 5
            values = [float(row["duration_ms"]) for row in mine
                      if low <= float(row["offset_s"]) < low + 5]
            series.append(round(statistics.median(values), 1) if values else None)
        return series

    def get_tour_sum(self, user):
        return round(sum(float(row["duration_ms"]) for row in self.get_user_rows(user)))

    def get_clock_medians(self, origin, buckets):
        """Median of ALL calls per 5 s bucket of the run clock -- the machine view."""
        series = []
        for index in range(buckets):
            low = origin + index * 5
            values = [float(row["duration_ms"]) for row in self.rows
                      if low <= float(row["wall"]) < low + 5]
            series.append(round(statistics.median(values), 1) if values else None)
        return series


class SecondLog:
    """One stack's per-second CSV: population, ingress rate, worker occupancy."""

    def __init__(self, path):
        self.path = path
        with open(path) as handle:
            self.rows = list(csv.DictReader(handle))

    @property
    def run_start(self):
        return min(float(row["wall"]) for row in self.rows)

    @property
    def run_seconds(self):
        return max(float(row["wall"]) for row in self.rows) - self.run_start

    def get_rows_in(self, low):
        return [row for row in self.rows if low <= float(row["wall"]) < low + 5]

    def get_rate_series(self, origin, buckets):
        """Mean ingress requests per second, per bucket."""
        series = []
        for index in range(buckets):
            values = [float(row["calls"]) for row in self.get_rows_in(origin + index * 5)]
            series.append(round(statistics.mean(values), 1) if values else 0.0)
        return series

    def get_field_series(self, field, origin, buckets, rounding=0):
        series = []
        for index in range(buckets):
            values = [float(row[field]) for row in self.get_rows_in(origin + index * 5)
                      if row[field] != ""]
            if not values:
                series.append(None)
            elif rounding:
                series.append(round(statistics.mean(values), rounding))
            else:
                series.append(round(statistics.mean(values)))
        return series

    def get_occupancy_series(self, origin, buckets):
        """Mean occupancy of every worker, per bucket: one list of lanes per bucket."""
        series = []
        for index in range(buckets):
            lanes = [row["occupancy_all"].split("|")
                     for row in self.get_rows_in(origin + index * 5)
                     if row["occupancy_all"]]
            if not lanes:
                series.append([])
                continue
            width = max(len(lane) for lane in lanes)
            means = []
            for worker in range(width):
                values = [float(lane[worker]) for lane in lanes if len(lane) > worker]
                means.append(round(statistics.mean(values), 1))
            series.append(means)
        return series


class StatsLog:
    """The container samples: memory in MiB, per container."""

    def __init__(self, path):
        self.path = path
        with open(path) as handle:
            self.rows = list(csv.DictReader(handle))

    def get_memory_series(self, container, origin, buckets):
        mine = [row for row in self.rows if row["container"].endswith(f"-{container}-1")]
        series = []
        for index in range(buckets):
            low = origin + index * 5
            values = [float(row["mem_mb"]) for row in mine
                      if low <= float(row["wall"]) < low + 5]
            series.append(round(statistics.mean(values)) if values else None)
        return series


class SiteNav:
    """The navigation bar shared by every page, with the current page marked."""

    PAGES = [
        ("index.html", "Home"),
        ("tipo1/README.html", "T1 · Method"),
        ("tipo1/findings.html", "T1 · Findings"),
        ("tipo1/run_base/baseline.html", "Baseline"),
        ("tipo1/run_8/eight_users.html", "8 users"),
        ("tipo1/run_16/sixteen_users.html", "16 users"),
        ("tipo1/run_32/thirtytwo_users.html", "32 users"),
        ("tipo1/machine_view.html", "Machine view"),
        ("tipo2/README.html", "T2 · Method"),
        ("tipo2/findings.html", "T2 · Findings"),
        ("tipo2/run_16/churn_16.html", "Churn 16"),
        ("tipo2/run_32/churn_32.html", "Churn 32"),
        ("tipo2/run_64/churn_64.html", "Churn 64"),
    ]

    def __init__(self, here, depth=2):
        self.here = here
        self.prefix = "../" * depth

    @property
    def html(self):
        links = []
        for path, label in self.PAGES:
            mark = ' class="here"' if path == self.here else ""
            links.append(f'<a href="{self.prefix}{path}"{mark}>{label}</a>')
        return '<nav class="sitenav">' + " ".join(links) + "</nav>"


class PageShell:
    """The chrome every page shares: fonts, palette, sticky nav, dark by default."""

    FONTS = ('<link rel="preconnect" href="https://fonts.googleapis.com">\n'
             '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?'
             'family=IBM+Plex+Sans:wght@400;600&family=IBM+Plex+Mono:wght@400;500'
             '&display=swap">')

    NAV_CSS = """<style>
  .sitenav { position: sticky; top: 0; z-index: 5; display: flex; flex-wrap: wrap;
    gap: 2px 14px; padding: 10px 24px; background: var(--card);
    border-bottom: 1px solid var(--grid);
    font-family: "IBM Plex Mono", monospace; font-size: 12px; }
  .sitenav a { color: var(--ink-2); text-decoration: none; }
  .sitenav a:hover { color: var(--ink); }
  .sitenav a.here { color: var(--ink); font-weight: 600; }
</style>"""

    def __init__(self, title, nav, palette, body_css):
        self.title = title
        self.nav = nav
        self.palette = palette
        self.body_css = body_css

    def get_html(self, body, script):
        return (f'<!doctype html>\n<html lang="en" data-theme="dark">\n<head>\n'
                f'<meta charset="utf-8">\n'
                f'<meta name="viewport" content="width=device-width, initial-scale=1">\n'
                f'<title>{self.title}</title>\n{self.FONTS}\n'
                f'<style>\n{self.palette}\n{self.body_css}\n</style>\n</head>\n<body>\n'
                f'{self.NAV_CSS}\n{self.nav.html}\n<main>\n{body}\n</main>\n'
                f'<script>\n{script}\n</script>\n</body>\n</html>\n')


class TourPage:
    """Type 1: one panel per user, his own tour against his stack's baseline."""

    PALETTE = """  :root {
    color-scheme: light;
    --surface: #fbfbf9; --card: #ffffff; --ink: #22272e; --ink-2: #5b6472;
    --ink-3: #8b93a1; --grid: #e7e9ec; --base: #2a78d6; --test: #eb6834;
    --good: #1e7d32; --bad: #c62828;
  }
  @media (prefers-color-scheme: dark) {
    :root:not([data-theme="light"]) {
      color-scheme: dark;
      --surface: #191b1e; --card: #202327; --ink: #e8eaed; --ink-2: #a8b0ba;
      --ink-3: #79818c; --grid: #33373d; --base: #3987e5; --test: #d95926;
      --good: #2e9e4f; --bad: #e66767;
    }
  }
  :root[data-theme="dark"] {
    color-scheme: dark;
    --surface: #191b1e; --card: #202327; --ink: #e8eaed; --ink-2: #a8b0ba;
    --ink-3: #79818c; --grid: #33373d; --base: #3987e5; --test: #d95926;
    --good: #2e9e4f; --bad: #e66767;
  }"""

    BODY_CSS = """  * { box-sizing: border-box; }
  body { margin: 0; background: var(--surface); color: var(--ink);
    font-family: "IBM Plex Sans", "Helvetica Neue", Arial, sans-serif; line-height: 1.5; }
  main { max-width: 1060px; margin: 0 auto; padding: 40px 24px 56px; }
  .eyebrow { font-family: "IBM Plex Mono", monospace; font-size: 12px;
    letter-spacing: 0.08em; text-transform: uppercase; color: var(--ink-3); margin: 0 0 6px; }
  h1 { font-size: 26px; font-weight: 600; margin: 0 0 4px; text-wrap: balance; }
  .sub { color: var(--ink-2); font-size: 14.5px; margin: 0 0 18px; max-width: 68ch; }
  .legend { display: flex; gap: 22px; flex-wrap: wrap; align-items: center;
    margin: 0 0 18px; font-size: 13px; color: var(--ink-2); }
  .legend svg { vertical-align: middle; }
  .grid8 { display: grid; grid-template-columns: repeat(auto-fit, minmax(430px, 1fr)); gap: 14px; }
  .panel { background: var(--card); border: 1px solid var(--grid); border-radius: 6px;
    padding: 12px 12px 4px; }
  .panel h2 { font-size: 13.5px; font-weight: 600; margin: 0 0 2px 6px;
    display: flex; justify-content: space-between; align-items: baseline; }
  .panel h2 small { font-family: "IBM Plex Mono", monospace; font-weight: 400;
    color: var(--ink-3); font-size: 11.5px; }
  .minigrid { display: grid; grid-template-columns: auto 1fr 1fr 1fr;
    gap: 1px 12px; margin: 4px 6px 8px; font-size: 11.5px;
    font-family: "IBM Plex Mono", monospace; font-variant-numeric: tabular-nums;
    color: var(--ink-2); }
  .minigrid .h { color: var(--ink-3); font-family: "IBM Plex Sans", sans-serif; }
  .minigrid .r { text-align: right; }
  .up { color: var(--bad); font-weight: 600; }
  .down { color: var(--good); font-weight: 600; }
  svg { display: block; width: 100%; height: auto; }
  .note { color: var(--ink-3); font-size: 13px; margin: 16px 2px 0; max-width: 75ch; }"""

    SCRIPT = """  const W = 500, H = 230, M = { top: 12, right: 34, bottom: 26, left: 38 };
  const NS = "http://www.w3.org/2000/svg";
  const yMax = D.y_max, buckets = D.buckets, span = buckets * 5 + 1;
  const x = b => M.left + ((b * 5 + 2.5) / span) * (W - M.left - M.right);
  const y = v => H - M.bottom - (Math.min(v, yMax) / yMax) * (H - M.top - M.bottom);
  const sign = v => (v > 0 ? "+" : "") + v + "%";
  const cls = v => v < 0 ? "down" : "up";
  const deltaPct = (stack, u) =>
    Math.round((D.sums[stack][u] - D.base_sum[stack]) / D.base_sum[stack] * 100);

  const monotoneSlopes = values => {
    const n = values.length, slopes = new Array(n);
    const delta = values.slice(0, -1).map((v, i) => (values[i + 1] - v) / 5);
    slopes[0] = delta[0]; slopes[n - 1] = delta[n - 2];
    for (let i = 1; i < n - 1; i++) {
      slopes[i] = delta[i - 1] * delta[i] <= 0 ? 0 : (delta[i - 1] + delta[i]) / 2;
    }
    return slopes;
  };
  const smoothPath = series => {
    const pts = [];
    series.forEach((v, b) => { if (v !== null) pts.push([b, v]); });
    if (pts.length < 2) return "";
    const values = pts.map(p => p[1]), slopes = monotoneSlopes(values);
    let d = `M${x(pts[0][0]).toFixed(1)},${y(values[0]).toFixed(1)}`;
    for (let i = 0; i < pts.length - 1; i++) {
      const x0 = x(pts[i][0]), x1 = x(pts[i + 1][0]), step = (x1 - x0) / 3;
      d += `C${(x0 + step).toFixed(1)},${y(values[i] + (5 / 3) * slopes[i]).toFixed(1)} `
         + `${(x1 - step).toFixed(1)},${y(values[i + 1] - (5 / 3) * slopes[i + 1]).toFixed(1)} `
         + `${x1.toFixed(1)},${y(values[i + 1]).toFixed(1)}`;
    }
    return d;
  };

  const panels = document.getElementById("panels");
  for (let u = 0; u < D.users; u++) {
    const panel = document.createElement("div");
    panel.className = "panel";
    const gap = Math.round((D.sums.bridge[u] - D.sums.legacy[u]) / D.sums.legacy[u] * 100);
    const baseGap = Math.round((D.base_sum.bridge - D.base_sum.legacy) / D.base_sum.legacy * 100);
    panel.innerHTML = `<h2><span>user ${u + 1}</span>`
      + `<small>enters at +${u * D.login_every}s</small></h2>`
      + `<div class="minigrid">`
      + `<span class="h"></span><span class="h r">gunicorn+gnrdaemon</span>`
      + `<span class="h r">genropy-asgi</span><span class="h r">delta</span>`
      + `<span class="h">with ${D.users} users</span><span class="r">${D.sums.legacy[u]} ms</span>`
      + `<span class="r">${D.sums.bridge[u]} ms</span>`
      + `<span class="r ${cls(gap)}">${sign(gap)}</span>`
      + `<span class="h">baseline, 1 user</span><span class="r">${D.base_sum.legacy} ms</span>`
      + `<span class="r">${D.base_sum.bridge} ms</span>`
      + `<span class="r ${cls(baseGap)}">${sign(baseGap)}</span>`
      + `<span class="h">vs baseline</span>`
      + `<span class="r ${cls(deltaPct("legacy", u))}">${sign(deltaPct("legacy", u))}</span>`
      + `<span class="r ${cls(deltaPct("bridge", u))}">${sign(deltaPct("bridge", u))}</span>`
      + `<span class="r"></span>`
      + `</div>`;
    const svg = document.createElementNS(NS, "svg");
    svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
    panel.appendChild(svg);
    panels.appendChild(panel);
    const el = (name, attrs) => {
      const node = document.createElementNS(NS, name);
      for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
      svg.appendChild(node);
      return node;
    };
    for (const reqs of [D.rate_scale / 2, D.rate_scale]) {
      const ry = H - M.bottom - (reqs / D.rate_scale) * (H - M.top - M.bottom) * 0.25;
      el("text", { x: M.left - 6, y: ry + 3, "text-anchor": "end",
                   fill: "var(--ink-3)", opacity: 0.9, "font-size": 8.5,
                   "font-family": "IBM Plex Mono, monospace" }).textContent = Math.round(reqs) + "r/s";
    }
    for (const pct of [35, 70]) {
      const py = H - M.bottom - (pct / 70) * (H - M.top - M.bottom) * 0.45;
      el("text", { x: W - M.right + 4, y: py + 3, fill: "#d55181",
                   opacity: 0.8, "font-size": 9,
                   "font-family": "IBM Plex Mono, monospace" }).textContent = pct + "%";
    }
    for (let v = 0; v <= yMax; v += yMax / 2) {
      el("line", { x1: M.left, x2: W - M.right, y1: y(v), y2: y(v),
                   stroke: "var(--grid)", "stroke-width": 1 });
      el("text", { x: M.left - 6, y: y(v) + 4, "text-anchor": "end",
                   fill: "var(--ink-3)", "font-size": 10,
                   "font-family": "IBM Plex Mono, monospace" }).textContent = v;
    }
    for (let s = 0; s <= buckets * 5 - 5; s += 10) {
      el("text", { x: x(s / 5) - ((s === 0) ? -2 : 2), y: H - M.bottom + 16,
                   "text-anchor": "middle", fill: "var(--ink-3)", "font-size": 10,
                   "font-family": "IBM Plex Mono, monospace" }).textContent = s;
    }
    D.ambient.bridge[u].forEach((rate, b) => {
      const x0 = x(b) - (x(1) - x(0)) / 2 + 1, x1 = x(b) + (x(1) - x(0)) / 2 - 1;
      const mean = (D.ambient.legacy[u][b] + rate) / 2;
      const top = H - M.bottom - (mean / D.rate_scale) * (H - M.top - M.bottom) * 0.25;
      el("rect", { x: x0, y: top, width: x1 - x0, height: H - M.bottom - top,
                   fill: "var(--ink-3)", opacity: 0.14 });
      const workers = D.occupancy[u][b] || [];
      const lane = (x1 - x0) / 6;
      workers.forEach((occ, w) => {
        const wtop = H - M.bottom - (occ / 70) * (H - M.top - M.bottom) * 0.45;
        el("rect", { x: x0 + w * lane + 0.5, y: wtop, width: lane - 1,
                     height: H - M.bottom - wtop, fill: "#d55181", opacity: 0.35 });
      });
    });
    const draw = (series, color, dash) => {
      const d = smoothPath(series);
      if (d) el("path", { d, fill: "none", stroke: color, "stroke-width": 2,
        ...(dash ? { "stroke-dasharray": "4 4" } : {}),
        "stroke-linejoin": "round", "stroke-linecap": "round" });
    };
    draw(D.baseline.legacy, "var(--base)", true);
    draw(D.baseline.bridge, "var(--base)", false);
    draw(D.run.legacy[u], "var(--test)", true);
    draw(D.run.bridge[u], "var(--test)", false);
  }"""

    def __init__(self, users, baseline, run, stats_note, login_every=3, buckets=11,
                 label=None, y_max=None):
        self.users = users
        self.baseline = baseline          # {"legacy": (CallLog, SecondLog), "bridge": ...}
        self.run = run                    # same shape, the N-user run
        self.stats_note = stats_note
        self.login_every = login_every
        self.buckets = buckets
        self.label = label or f"{users} users"
        self.forced_y_max = y_max

    @property
    def stacks(self):
        return ("legacy", "bridge")

    @property
    def user_range(self):
        return range(1, self.users + 1)

    @property
    def baseline_series(self):
        return {name: self.baseline[name][0].get_tour_buckets(1, self.buckets)
                for name in self.stacks}

    @property
    def baseline_sums(self):
        return {name: self.baseline[name][0].get_tour_sum(1) for name in self.stacks}

    @property
    def run_series(self):
        return {name: [self.run[name][0].get_tour_buckets(user, self.buckets)
                       for user in self.user_range] for name in self.stacks}

    @property
    def run_sums(self):
        return {name: [self.run[name][0].get_tour_sum(user) for user in self.user_range]
                for name in self.stacks}

    @property
    def ambient_series(self):
        series = {}
        for name in self.stacks:
            calls, seconds = self.run[name]
            series[name] = [seconds.get_rate_series(calls.get_window_start(user), self.buckets)
                            for user in self.user_range]
        return series

    @property
    def occupancy_series(self):
        calls, seconds = self.run["bridge"]
        return [seconds.get_occupancy_series(calls.get_window_start(user), self.buckets)
                for user in self.user_range]

    def get_y_max(self, baseline, run):
        if self.forced_y_max:
            return self.forced_y_max
        values = [v for series in list(baseline.values()) + [s for rows in run.values() for s in rows]
                  for v in series if v is not None]
        values.sort()
        p95 = values[int(len(values) * 0.95)]
        return max(50, int((p95 + 49) // 50) * 50)

    @property
    def data(self):
        baseline, run = self.baseline_series, self.run_series
        ambient = self.ambient_series
        peak_rate = max(v for rows in ambient.values() for series in rows for v in series)
        return {
            "users": self.users, "login_every": self.login_every, "buckets": self.buckets,
            "baseline": baseline, "run": run,
            "sums": self.run_sums, "base_sum": self.baseline_sums,
            "ambient": ambient, "occupancy": self.occupancy_series,
            "y_max": self.get_y_max(baseline, run),
            "rate_scale": max(10, int((peak_rate + 4) // 5) * 5),
        }

    def get_thousands(self, value):
        return f"{value:,}".replace(",", "&nbsp;")

    def get_body(self, data):
        totals = {name: sum(data["sums"][name]) for name in self.stacks}
        saved = round((totals["bridge"] - totals["legacy"]) / totals["legacy"] * 100)
        legend = "\n".join(
            f'    <span><svg width="34" height="10"><line x1="0" y1="5" x2="34" y2="5" '
            f'stroke="var(--{colour})" stroke-width="2"{dash}/></svg> {text}</span>'
            for colour, dash, text in [
                ("base", "", "genropy-asgi · baseline"),
                ("base", ' stroke-dasharray="4 4"', "gunicorn+gnrdaemon · baseline"),
                ("test", "", f"genropy-asgi · with {self.users} users"),
                ("test", ' stroke-dasharray="4 4"', f"gunicorn+gnrdaemon · with {self.users} users"),
            ])
        return f"""  <p class="eyebrow">{self.users} users · one every {self.login_every} s · speed ×2 · idle capped at 3 s · hetzner ccx43 lab 2026-08-27</p>
  <h1>{self.label.capitalize()} against the baseline, on dedicated cores</h1>
  <p class="sub">Each panel is one user: the median of his response times per 5-second
  interval of HIS tour, compared with the single-user baseline of the same stack.
  User 1 enters alone and is joined by the others; user {self.users} enters at full
  population. Tour sums: gunicorn+gnrdaemon baseline {data["base_sum"]["legacy"]} ms,
  genropy-asgi baseline {data["base_sum"]["bridge"]} ms. {self.stats_note}</p>

  <div class="legend" style="gap:12px; margin-bottom:12px">
    <span class="panel" style="padding:10px 16px">gunicorn+gnrdaemon · total wait, {self.users} tours&nbsp;
      <b style="font-family:'IBM Plex Mono',monospace">{self.get_thousands(totals["legacy"])} ms</b></span>
    <span class="panel" style="padding:10px 16px">genropy-asgi · total wait, {self.users} tours&nbsp;
      <b style="font-family:'IBM Plex Mono',monospace">{self.get_thousands(totals["bridge"])} ms</b></span>
    <span class="panel" style="padding:10px 16px">genropy-asgi makes you wait&nbsp;
      <b style="font-family:'IBM Plex Mono',monospace">{"&minus;" if saved < 0 else "+"}{abs(saved)}%</b></span>
  </div>
  <div class="legend">
{legend}
  </div>

  <div class="grid8" id="panels"></div>

  <p class="note">Solid line = genropy-asgi, dashed = gunicorn+gnrdaemon; blue = single-user
  baseline, orange = the {self.users}-user run. The panels' time axis is the user's own tour,
  the same for everyone: the same interval holds the same calls. In the grid, green = genropy-asgi
  makes you wait less, red = more; the "vs baseline" row uses each stack's own single-user
  baseline as denominator. The gray bars at the bottom are the ingress requests per second
  measured in THAT user's tour window, mean of the two stacks: own scale, {data["rate_scale"]} req/s
  = a quarter of the panel height. Overlaid, pink and translucent, the mean occupancy of the
  genropy-asgi workers in that interval — one column per worker, from the left, up to 6 — on the
  pink right scale 0-70%, kept below half the panel. The gray "r/s" ticks on the left mark the
  bars' band scale; the black numbers are the curves' milliseconds.</p>"""

    def get_html(self, nav):
        data = self.data
        shell = PageShell(f"{self.label.capitalize()} Hetzner", nav, self.PALETTE, self.BODY_CSS)
        script = f"  const D = {json.dumps(data)};\n{self.SCRIPT}"
        return shell.get_html(self.get_body(data), script)


class ChurnPage:
    """Type 2: the machine's view of a churn run -- five strips on the run clock."""

    PALETTE = """  :root {
    color-scheme: light;
    --surface: #fbfbf9; --card: #ffffff; --ink: #22272e; --ink-2: #5b6472;
    --ink-3: #8b93a1; --grid: #e7e9ec; --legacy: #1565c0; --bridge: #1e7d32;
  }
  @media (prefers-color-scheme: dark) {
    :root:not([data-theme="light"]) {
      color-scheme: dark;
      --surface: #191b1e; --card: #202327; --ink: #e8eaed; --ink-2: #a8b0ba;
      --ink-3: #79818c; --grid: #33373d; --legacy: #3987e5; --bridge: #2e9e4f;
    }
  }
  :root[data-theme="dark"] {
      color-scheme: dark;
      --surface: #191b1e; --card: #202327; --ink: #e8eaed; --ink-2: #a8b0ba;
      --ink-3: #79818c; --grid: #33373d; --legacy: #3987e5; --bridge: #2e9e4f;
  }"""

    BODY_CSS = """  * { box-sizing: border-box; }
  body { margin: 0; background: var(--surface); color: var(--ink);
    font-family: "IBM Plex Sans", "Helvetica Neue", Arial, sans-serif; line-height: 1.6; }
  main { max-width: 960px; margin: 0 auto; padding: 24px 24px 64px; }
  .eyebrow { font-family: "IBM Plex Mono", monospace; font-size: 12px;
    letter-spacing: 0.08em; text-transform: uppercase; color: var(--ink-3); margin: 14px 0 6px; }
  h1 { font-size: 26px; font-weight: 600; margin: 0 0 6px; }
  .sub { color: var(--ink-2); font-size: 14.5px; margin: 0 0 18px; max-width: 70ch; }
  .legend { display: flex; gap: 22px; margin: 0 0 14px; font-size: 13px;
    color: var(--ink-2); flex-wrap: wrap; }
  .panel { background: var(--card); border: 1px solid var(--grid); border-radius: 6px;
    padding: 14px 14px 6px; margin-bottom: 14px; }
  .panel h2 { font-size: 14px; font-weight: 600; margin: 0 0 6px 4px; }
  svg { display: block; width: 100%; height: auto; }
  .note { color: var(--ink-3); font-size: 13px; margin: 14px 2px 0; max-width: 76ch; }"""

    SCRIPT = """  const NB = D.buckets, W = 920;
  const NS = "http://www.w3.org/2000/svg";
  const ML = 46, MR = 14;
  const x = b => ML + ((b * 5 + 2.5) / (NB * 5)) * (W - ML - MR);
  function strip(id, H, yMax, ticks, unit) {
    const svg = document.getElementById(id);
    const top = 12, bottom = 24;
    const y = v => H - bottom - (v / yMax) * (H - top - bottom);
    const el = (name, attrs) => {
      const node = document.createElementNS(NS, name);
      for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
      svg.appendChild(node);
      return node;
    };
    for (const t of ticks) {
      el("line", { x1: ML, x2: W - MR, y1: y(t), y2: y(t), stroke: "var(--grid)", "stroke-width": 1 });
      el("text", { x: ML - 8, y: y(t) + 4, "text-anchor": "end", fill: "var(--ink-3)",
                   "font-size": 11, "font-family": "IBM Plex Mono, monospace" }).textContent = t;
    }
    for (let s = 0; s <= NB * 5; s += 25) {
      el("text", { x: x(s / 5 - 0.5), y: H - bottom + 16, "text-anchor": "middle",
                   fill: "var(--ink-3)", "font-size": 10,
                   "font-family": "IBM Plex Mono, monospace" }).textContent = s;
    }
    el("text", { x: W - MR, y: top - 1, "text-anchor": "end", fill: "var(--ink-3)",
                 "font-size": 10, "font-family": "IBM Plex Mono, monospace" }).textContent = unit;
    return { el, y };
  }
  function line(ctx, values, color, dash) {
    const pts = [];
    values.forEach((v, b) => { if (v !== null) pts.push([b, v]); });
    if (pts.length < 2) return;
    const n = pts.length, slopes = new Array(n);
    const delta = pts.slice(0, -1).map((pt, i) =>
      (pts[i + 1][1] - pt[1]) / (pts[i + 1][0] - pt[0]));
    slopes[0] = delta[0]; slopes[n - 1] = delta[n - 2];
    for (let i = 1; i < n - 1; i++) {
      slopes[i] = delta[i - 1] * delta[i] <= 0 ? 0 : (delta[i - 1] + delta[i]) / 2;
    }
    let d = `M${x(pts[0][0]).toFixed(1)},${ctx.y(pts[0][1]).toFixed(1)}`;
    for (let i = 0; i < n - 1; i++) {
      const [b0, v0] = pts[i], [b1, v1] = pts[i + 1];
      const step = (b1 - b0) / 3;
      d += `C${x(b0 + step).toFixed(1)},${ctx.y(v0 + step * slopes[i]).toFixed(1)} `
         + `${x(b1 - step).toFixed(1)},${ctx.y(v1 - step * slopes[i + 1]).toFixed(1)} `
         + `${x(b1).toFixed(1)},${ctx.y(v1).toFixed(1)}`;
    }
    ctx.el("path", { d, fill: "none", stroke: color, "stroke-width": 2,
                    ...(dash ? { "stroke-dasharray": "5 5" } : {}),
                    "stroke-linejoin": "round", "stroke-linecap": "round" });
  }
  const half = v => [0, Math.round(v / 2), v];
  let c = strip("med", 260, D.med_max, half(D.med_max), "ms");
  line(c, D.med_l, "var(--legacy)", true); line(c, D.med_b, "var(--bridge)", false);
  c = strip("pop", 120, D.pop_max, half(D.pop_max), "users");
  line(c, D.pop_l, "var(--legacy)", true); line(c, D.pop_b, "var(--bridge)", false);
  c = strip("rate", 120, D.rate_max, half(D.rate_max), "req/s");
  line(c, D.rate_l, "var(--legacy)", true); line(c, D.rate_b, "var(--bridge)", false);
  c = strip("occ", 130, 70, [0, 35, 70], "%");
  D.occ.forEach((workers, b) => {
    const bw = (x(1) - x(0));
    const lane = (bw - 2) / 6;
    workers.forEach((v, w) => {
      const yv = c.y(v);
      c.el("rect", { x: x(b) - bw / 2 + 1 + w * lane, y: yv, width: lane - 1,
                     height: c.y(0) - yv, fill: "#d55181", opacity: 0.4 });
    });
  });
  c = strip("mem", 130, D.mem_max, [Math.round(D.mem_max / 4), Math.round(D.mem_max / 2),
                                    Math.round(D.mem_max * 3 / 4), D.mem_max], "MiB");
  line(c, D.mem_l, "var(--legacy)", true); line(c, D.mem_b, "var(--bridge)", false);"""

    def __init__(self, peak, run, stats, entries, churn_every=5, drain_every=3):
        self.peak = peak
        self.run = run                    # {"legacy": (CallLog, SecondLog), "bridge": ...}
        self.stats = stats                # StatsLog or None
        self.entries = entries            # total entries the driver reported, per stack
        self.churn_every = churn_every
        self.drain_every = drain_every

    @property
    def buckets(self):
        return max(int(self.run[name][1].run_seconds // 5) + 1 for name in ("legacy", "bridge"))

    def get_ceiling(self, values, step):
        peak = max([v for v in values if v is not None] or [0])
        return max(step, int((peak + step - 1) // step) * step)

    @property
    def data(self):
        nb = self.buckets
        data = {"buckets": nb, "peak": self.peak}
        for name, tag in (("legacy", "l"), ("bridge", "b")):
            calls, seconds = self.run[name]
            origin = seconds.run_start
            data[f"med_{tag}"] = calls.get_clock_medians(origin, nb)
            data[f"rate_{tag}"] = seconds.get_rate_series(origin, nb)
            data[f"pop_{tag}"] = seconds.get_field_series("users_in", origin, nb)
            data[f"mem_{tag}"] = (self.stats.get_memory_series(name, origin, nb)
                                  if self.stats else [None] * nb)
        data["occ"] = self.run["bridge"][1].get_occupancy_series(
            self.run["bridge"][1].run_start, nb)
        data["med_max"] = self.get_ceiling(data["med_l"] + data["med_b"], 20)
        data["pop_max"] = self.get_ceiling(data["pop_l"] + data["pop_b"], 2)
        data["rate_max"] = self.get_ceiling(data["rate_l"] + data["rate_b"], 20)
        data["mem_max"] = self.get_ceiling(data["mem_l"] + data["mem_b"], 100)
        return data

    def get_body(self, data):
        entries = " / ".join(f"{count} on {name}" for name, count in self.entries.items())
        return f"""  <p class="eyebrow">churn · peak {self.peak} · entry every 3 s · turnover draw every {self.churn_every} s · closure draw every {self.drain_every} s · speed ×2 · hetzner ccx43 · 2026-08-27</p>
  <h1>Churn to peak {self.peak}</h1>
  <p class="sub">Machine view: the median response time of ALL calls per
  5-second interval on the run's clock. Total entries for a peak of {self.peak}:
  {entries} (drawn users left and re-entered during the climb).
  gunicorn+gnrdaemon dashed, genropy-asgi solid.</p>
  <div class="legend">
    <span><svg width="34" height="10"><line x1="0" y1="5" x2="34" y2="5" stroke="var(--legacy)" stroke-width="2" stroke-dasharray="5 5"/></svg> gunicorn+gnrdaemon</span>
    <span><svg width="34" height="10"><line x1="0" y1="5" x2="34" y2="5" stroke="var(--bridge)" stroke-width="2"/></svg> genropy-asgi</span>
  </div>
  <div class="panel"><h2>Median of all calls (ms)</h2><svg id="med" viewBox="0 0 920 260"></svg></div>
  <div class="panel"><h2>Population (users in)</h2><svg id="pop" viewBox="0 0 920 120"></svg></div>
  <div class="panel"><h2>Ingress requests per second</h2><svg id="rate" viewBox="0 0 920 120"></svg></div>
  <div class="panel"><h2>genropy-asgi worker occupancy (%)</h2><svg id="occ" viewBox="0 0 920 130"></svg></div>
  <div class="panel"><h2>Container memory (MiB)</h2><svg id="mem" viewBox="0 0 920 130"></svg></div>
  <p class="note">All strips share the run clock (x, seconds). Population and
  req/s are near-identical on the two stacks by construction; memory and the
  median are where they differ. The pink columns are the genropy-asgi workers'
  occupancy, one column per worker, scale 0-70%. Raw data beside this page.</p>"""

    def get_html(self, nav):
        data = self.data
        shell = PageShell(f"Churn, peak {self.peak}", nav, self.PALETTE, self.BODY_CSS)
        script = f"  const D = {json.dumps(data)};\n{self.SCRIPT}"
        return shell.get_html(self.get_body(data), script)


class ReportSite:
    """Command line: build one page from the CSV files of the two stacks."""

    def __init__(self):
        self.parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
        subs = self.parser.add_subparsers(dest="page", required=True)
        tour = subs.add_parser("tour", help="type 1 page, one panel per user")
        tour.add_argument("--baseline", nargs=2, required=True, metavar=("LEGACY", "BRIDGE"),
                          help="the two baseline runs, without the _calls.csv suffix")
        tour.add_argument("--run", nargs=2, required=True, metavar=("LEGACY", "BRIDGE"))
        tour.add_argument("--users", type=int, required=True)
        tour.add_argument("--label", default=None)
        tour.add_argument("--login-every", type=int, default=3)
        tour.add_argument("--y-max", type=int, default=None)
        tour.add_argument("--here", required=True, help="this page's path in the nav")
        tour.add_argument("--out", required=True)
        churn = subs.add_parser("churn", help="type 2 page, the machine view")
        churn.add_argument("--run", nargs=2, required=True, metavar=("LEGACY", "BRIDGE"))
        churn.add_argument("--peak", type=int, required=True)
        churn.add_argument("--stats", default=None)
        churn.add_argument("--entries", nargs=2, type=int, default=[0, 0],
                           metavar=("LEGACY", "BRIDGE"))
        churn.add_argument("--here", required=True)
        churn.add_argument("--out", required=True)

    def get_pair(self, stem):
        return CallLog(f"{stem}_calls.csv"), SecondLog(f"{stem}_seconds.csv")

    def get_stacks(self, pair):
        return {"legacy": self.get_pair(pair[0]), "bridge": self.get_pair(pair[1])}

    def build(self, argv=None):
        args = self.parser.parse_args(argv)
        depth = args.here.count("/")
        nav = SiteNav(args.here, depth)
        if args.page == "tour":
            page = TourPage(
                users=args.users,
                baseline=self.get_stacks(args.baseline),
                run=self.get_stacks(args.run),
                stats_note="Clean runs: restart and one discarded warm-up tour before every measurement.",
                login_every=args.login_every, label=args.label, y_max=args.y_max)
        else:
            page = ChurnPage(
                peak=args.peak, run=self.get_stacks(args.run),
                stats=StatsLog(args.stats) if args.stats else None,
                entries={"gunicorn+gnrdaemon": args.entries[0], "genropy-asgi": args.entries[1]})
        with open(args.out, "w") as handle:
            handle.write(page.get_html(nav))
        print(f"written {args.out}")


if __name__ == "__main__":
    ReportSite().build()
