"""Turn two churn runs into one report: a page of charts and a table of facts.

Reads, for each stack, the driver's per-second CSV and the `docker stats`
stream taken beside it, merges them on the wall clock, and writes a
self-contained HTML page — inline SVG, no libraries, nothing fetched.

  python3 churn_report.py \
      --run bridge:docker/runtime/bridge_churn.csv:docker/runtime/bridge_churn_stats.csv \
      --run legacy:docker/runtime/legacy_churn.csv:docker/runtime/legacy_churn_stats.csv \
      --out docker/runtime/churn_report.html

Each ``--run`` is ``label:driver_csv:stats_csv``; the stats file may be left
empty (``label:driver.csv:``) when a run has none.
"""

import argparse
import csv
import html
import statistics

PALETTE = {"bridge": "#2b7bba", "legacy": "#c8622a"}
FALLBACK = ["#6a8f3c", "#8a5fa8", "#b03050"]

CHARTS = [
    ("users_in", "Users logged in", "users", 1),
    ("calls", "Calls served per second", "calls/s", 1),
    ("p50_ms", "Latency p50", "ms", 1),
    ("p99_ms", "Latency p99", "ms", 1),
    ("mem_mb", "Memory of the whole stack", "MB", 1),
    ("cpu_pct", "CPU of the whole stack", "% of one core", 1),
    ("workers", "Worker processes", "workers", 1),
    ("occupancy_max", "Occupancy the pool reads (fullest worker)", "%", 1),
]


class Series:
    """One stack's run: the per-second rows, already merged with its stats."""

    def __init__(self, label, driver_path, stats_path):
        self.label = label
        self.rows = self.read_driver(driver_path)
        if stats_path:
            self.merge_stats(stats_path)
        self.rebase_time()

    def read_driver(self, path):
        with open(path) as handle:
            return [row for row in csv.DictReader(handle) if row.get("t")]

    def merge_stats(self, path):
        """Sum cpu and memory of the stack's containers, per wall-clock second.

        Postgres is left out: it is the same server for both stacks and its
        cost belongs to neither.
        """
        by_second = {}
        with open(path) as handle:
            for row in csv.DictReader(handle):
                name = row["container"]
                if "postgres" in name or self.label not in name:
                    continue
                second = int(float(row["wall"]))
                entry = by_second.setdefault(second, {"cpu": 0.0, "mem": 0.0})
                entry["cpu"] += float(row["cpu_pct"] or 0)
                entry["mem"] += float(row["mem_mb"] or 0)
        # `docker stats --no-stream` takes over a second to answer, so the
        # stream skips seconds. A gap of a second or two is filled from the
        # nearest reading, which is what the value was: memory and cpu do not
        # teleport between samples. A wider gap is left blank.
        for row in self.rows:
            second = int(float(row["wall"]))
            entry = by_second.get(second)
            if entry is None:
                for offset in (1, -1, 2, -2, 3, -3):
                    entry = by_second.get(second + offset)
                    if entry is not None:
                        break
            row["cpu_pct"] = f"{entry['cpu']:.1f}" if entry else ""
            row["mem_mb"] = f"{entry['mem']:.1f}" if entry else ""

    def rebase_time(self):
        """Both runs start at zero, so the two curves lie over each other."""
        if not self.rows:
            return
        origin = float(self.rows[0]["t"])
        for row in self.rows:
            row["t"] = f"{float(row['t']) - origin:.0f}"

    def points(self, column):
        """(t, value) for the rows where that column has a number."""
        out = []
        for row in self.rows:
            value = row.get(column, "")
            if value not in ("", None):
                try:
                    out.append((float(row["t"]), float(value)))
                except ValueError:
                    pass
        return out

    def phase_bounds(self):
        """(phase, t_start, t_end) for each phase, in order."""
        bounds, current, start = [], None, 0.0
        for row in self.rows:
            if row["phase"] != current:
                if current is not None:
                    bounds.append((current, start, float(row["t"])))
                current, start = row["phase"], float(row["t"])
        if current is not None:
            bounds.append((current, start, float(self.rows[-1]["t"])))
        return bounds

    def column_max(self, column):
        values = [value for _, value in self.points(column)]
        return max(values) if values else 0.0

    def total(self, column):
        return sum(value for _, value in self.points(column))

    def percentile_over_run(self, column, which):
        values = sorted(value for _, value in self.points(column))
        if not values:
            return None
        if len(values) == 1:
            return values[0]
        return statistics.quantiles(values, n=100)[which - 1]

    def band_median(self, column, low, high):
        """The median of *column* over the seconds whose population was in band.

        A single second's value — "the latency at the peak" — is noise: one
        second holds a few dozen calls and the population is at its peak for
        one second only. Comparing two stacks second by second lets chance pick
        the winner. A band holds a hundred seconds or more of the same load,
        and that is comparable.
        """
        values = sorted(
            float(row[column])
            for row in self.rows
            if row.get(column) not in ("", None)
            and low <= float(row.get("users_in") or 0) <= high)
        return statistics.median(values) if values else None

    def at_peak(self, column):
        """The value of *column* in the second where the population was fullest."""
        best_t, best_users = None, -1
        for row in self.rows:
            users = float(row.get("users_in") or 0)
            if users > best_users:
                best_users, best_t = users, row["t"]
        for row in self.rows:
            if row["t"] == best_t and row.get(column) not in ("", None):
                return float(row[column])
        return None


class Chart:
    """One inline SVG line chart, every series on the same axes."""

    WIDTH, HEIGHT = 900, 260
    LEFT, RIGHT, TOP, BOTTOM = 62, 18, 26, 34

    def __init__(self, column, title, unit, series_list):
        self.column = column
        self.title = title
        self.unit = unit
        self.series_list = [s for s in series_list if s.points(column)]

    @property
    def x_max(self):
        return max((max(t for t, _ in s.points(self.column)) for s in self.series_list),
                   default=1.0) or 1.0

    @property
    def y_max(self):
        top = max((s.column_max(self.column) for s in self.series_list), default=1.0)
        return top * 1.12 or 1.0

    def to_x(self, value):
        span = self.WIDTH - self.LEFT - self.RIGHT
        return self.LEFT + span * value / self.x_max

    def to_y(self, value):
        span = self.HEIGHT - self.TOP - self.BOTTOM
        return self.HEIGHT - self.BOTTOM - span * value / self.y_max

    def colour(self, index, label):
        return PALETTE.get(label, FALLBACK[index % len(FALLBACK)])

    def grid(self):
        parts = []
        for step in range(5):
            value = self.y_max * step / 4
            y = self.to_y(value)
            parts.append(f'<line x1="{self.LEFT}" y1="{y:.1f}" x2="{self.WIDTH-self.RIGHT}" '
                         f'y2="{y:.1f}" class="grid"/>')
            parts.append(f'<text x="{self.LEFT-8}" y="{y+4:.1f}" class="ylab">'
                         f'{value:.0f}</text>')
        for step in range(7):
            value = self.x_max * step / 6
            x = self.to_x(value)
            parts.append(f'<text x="{x:.1f}" y="{self.HEIGHT-12}" class="xlab">'
                         f'{value:.0f}s</text>')
        return "".join(parts)

    def phase_bands(self):
        """Shade the phases of the FIRST series — both runs share the shape."""
        if not self.series_list:
            return ""
        parts = []
        for phase, start, end in self.series_list[0].phase_bounds():
            if phase not in ("hold",):
                continue
            x1, x2 = self.to_x(start), self.to_x(end)
            parts.append(f'<rect x="{x1:.1f}" y="{self.TOP}" width="{max(x2-x1,1):.1f}" '
                         f'height="{self.HEIGHT-self.TOP-self.BOTTOM}" class="band"/>')
            parts.append(f'<text x="{(x1+x2)/2:.1f}" y="{self.TOP+14}" class="band-label">'
                         f'hold</text>')
        return "".join(parts)

    def lines(self):
        parts = []
        for index, series in enumerate(self.series_list):
            points = series.points(self.column)
            path = " ".join(f"{self.to_x(t):.1f},{self.to_y(v):.1f}" for t, v in points)
            parts.append(f'<polyline points="{path}" fill="none" '
                         f'stroke="{self.colour(index, series.label)}" stroke-width="1.8"/>')
        return "".join(parts)

    def legend(self):
        parts = []
        for index, series in enumerate(self.series_list):
            x = self.LEFT + index * 130
            colour = self.colour(index, series.label)
            parts.append(f'<rect x="{x}" y="6" width="11" height="11" fill="{colour}"/>')
            parts.append(f'<text x="{x+16}" y="16" class="legend">'
                         f'{html.escape(series.label)}</text>')
        return "".join(parts)

    def render(self):
        if not self.series_list:
            return f"<p class='missing'>{html.escape(self.title)}: no data.</p>"
        return (f'<figure><figcaption>{html.escape(self.title)} '
                f'<span class="unit">({html.escape(self.unit)})</span></figcaption>'
                f'<svg viewBox="0 0 {self.WIDTH} {self.HEIGHT}" '
                f'preserveAspectRatio="xMidYMid meet" role="img">'
                f'{self.phase_bands()}{self.grid()}{self.lines()}{self.legend()}'
                f'</svg></figure>')


class BarChart:
    """Paired bars per population bucket: the current stack against the term.

    Bars read better than lines when the x axis is a population and not time:
    each bucket is one question — "at N users, who costs what?" — and the two
    answers stand side by side.
    """

    WIDTH, HEIGHT = 900, 280
    LEFT, RIGHT, TOP, BOTTOM = 62, 18, 30, 40
    COLOURS = ("#1e7d32", "#1565c0")

    def __init__(self, title, unit, buckets, pairs, digits=0):
        self.title = title
        self.unit = unit
        self.buckets = buckets
        self.pairs = pairs
        self.digits = digits

    @property
    def y_max(self):
        top = max((v for pair in self.pairs for v in pair if v is not None), default=1.0)
        return (top or 1.0) * 1.15

    def to_y(self, value):
        span = self.HEIGHT - self.TOP - self.BOTTOM
        return self.HEIGHT - self.BOTTOM - span * value / self.y_max

    def render(self, labels):
        if not any(v is not None for pair in self.pairs for v in pair):
            return ""
        span = self.WIDTH - self.LEFT - self.RIGHT
        group = span / len(self.buckets)
        bar = group * 0.36
        parts = []
        for step in range(5):
            value = self.y_max * step / 4
            y = self.to_y(value)
            parts.append(f'<line x1="{self.LEFT}" y1="{y:.1f}" '
                         f'x2="{self.WIDTH-self.RIGHT}" y2="{y:.1f}" class="grid"/>')
            parts.append(f'<text x="{self.LEFT-8}" y="{y+4:.1f}" class="ylab">'
                         f'{value:.0f}</text>')
        for index, (bucket, pair) in enumerate(zip(self.buckets, self.pairs)):
            x0 = self.LEFT + group * index + group * 0.12
            for side, value in enumerate(pair):
                if value is None:
                    continue
                x = x0 + side * bar
                y = self.to_y(value)
                parts.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar-2:.1f}" '
                             f'height="{self.HEIGHT-self.BOTTOM-y:.1f}" '
                             f'fill="{self.COLOURS[side]}"/>')
            parts.append(f'<text x="{x0+bar:.1f}" y="{self.HEIGHT-22}" class="xlab">'
                         f'{bucket}</text>')
        for side, label in enumerate(labels):
            x = self.LEFT + side * 130
            parts.append(f'<rect x="{x}" y="8" width="11" height="11" '
                         f'fill="{self.COLOURS[side]}"/>')
            parts.append(f'<text x="{x+16}" y="18" class="legend">'
                         f'{html.escape(label)}</text>')
        parts.append(f'<text x="{self.WIDTH/2:.0f}" y="{self.HEIGHT-6}" class="xlab">users</text>')
        return (f'<figure><figcaption>{html.escape(self.title)} '
                f'<span class="unit">({html.escape(self.unit)})</span></figcaption>'
                f'<svg viewBox="0 0 {self.WIDTH} {self.HEIGHT}" role="img">'
                f'{"".join(parts)}</svg></figure>')


class Report:
    """The whole page: the facts table first, then the charts."""

    def __init__(self, series_list, conditions):
        self.series_list = series_list
        self.conditions = conditions

    def fact_rows(self):
        """One row per measured fact, one column per stack."""
        def fmt(value, digits=1):
            return "—" if value is None else f"{value:,.{digits}f}".replace(",", " ")

        facts = [
            ("Peak users logged in", lambda s: fmt(s.column_max("users_in"), 0)),
            ("Logins performed", lambda s: fmt(s.total("logins"), 0)),
            ("Logouts performed", lambda s: fmt(s.total("logouts"), 0)),
            ("Calls served, whole run", lambda s: fmt(s.total("calls"), 0)),
            ("Errors, whole run", lambda s: fmt(s.total("errors"), 0)),
            ("Peak calls per second", lambda s: fmt(s.column_max("calls"), 0)),
            ("Latency p99, worst second", lambda s: fmt(s.column_max("p99_ms"))),
            ("Peak memory, whole stack (MB)", lambda s: fmt(s.column_max("mem_mb"))),
            ("Memory at the peak (MB)", lambda s: fmt(s.at_peak("mem_mb"))),
            ("Peak CPU (% of one core)", lambda s: fmt(s.column_max("cpu_pct"))),
            ("Worker processes at the peak", lambda s: fmt(s.at_peak("workers"), 0)),
            ("Peak worker processes", lambda s: fmt(s.column_max("workers"), 0)),
            ("Occupancy the pool read, peak", lambda s: fmt(s.column_max("occupancy_max"))),
        ]
        rows = []
        for name, getter in facts:
            values = [getter(series) for series in self.series_list]
            if all(value == "—" for value in values):
                continue
            rows.append((name, values))
        return rows

    BANDS = ((1, 32), (33, 64), (65, 128), (129, 200), (201, 256))

    def band_table(self):
        """Latency per population band — the comparison that is actually fair."""
        heads = "".join(f"<th>{html.escape(s.label)} p50</th>"
                        f"<th>{html.escape(s.label)} p99</th>" for s in self.series_list)
        body = []
        for low, high in self.BANDS:
            cells = []
            has_any = False
            for series in self.series_list:
                for column in ("p50_ms", "p99_ms"):
                    value = series.band_median(column, low, high)
                    has_any = has_any or value is not None
                    cells.append(f"<td>{'—' if value is None else f'{value:.1f}'}</td>")
            if has_any:
                body.append(f"<tr><th scope='row'>{low}–{high} users</th>"
                            + "".join(cells) + "</tr>")
        return (f'<div class="wrap"><table><thead><tr><th scope="col">Population</th>'
                f'{heads}</tr></thead><tbody>{"".join(body)}</tbody></table></div>')

    RESOURCE_COLUMNS = (("mem_mb", "memory MB"), ("cpu_pct", "cpu %"),
                        ("workers", "workers"), ("occupancy_max", "occupancy %"))

    def resource_table(self):
        """What each population band COSTS: memory, cpu, processes, occupancy."""
        heads = "".join(
            f"<th>{html.escape(series.label)} {html.escape(title)}</th>"
            for series in self.series_list for _, title in self.RESOURCE_COLUMNS)
        body = []
        for low, high in self.BANDS:
            cells, has_any = [], False
            for series in self.series_list:
                for column, _ in self.RESOURCE_COLUMNS:
                    value = series.band_median(column, low, high)
                    has_any = has_any or value is not None
                    digits = 0 if column == "workers" else 1
                    cells.append("<td>" + ("—" if value is None
                                           else f"{value:.{digits}f}") + "</td>")
            if has_any:
                body.append(f"<tr><th scope='row'>{low}–{high} users</th>"
                            + "".join(cells) + "</tr>")
        return (f'<div class="wrap"><table><thead><tr><th scope="col">Population</th>'
                f'{heads}</tr></thead><tbody>{"".join(body)}</tbody></table></div>')

    def phase_table(self):
        """The three phases side by side: what each cost while it lasted."""
        heads = "".join(f"<th>{html.escape(s.label)} calls/s</th>"
                        f"<th>{html.escape(s.label)} p50 ms</th>"
                        f"<th>{html.escape(s.label)} memory MB</th>"
                        for s in self.series_list)
        body = []
        for phase in ("climb", "hold", "drain"):
            cells, has_any = [], False
            for series in self.series_list:
                rows = [row for row in series.rows if row["phase"] == phase]
                for column, digits in (("calls", 0), ("p50_ms", 1), ("mem_mb", 1)):
                    values = sorted(float(row[column]) for row in rows
                                    if row.get(column) not in ("", None))
                    value = statistics.median(values) if values else None
                    has_any = has_any or value is not None
                    cells.append("<td>" + ("—" if value is None
                                           else f"{value:.{digits}f}") + "</td>")
            if has_any:
                body.append(f"<tr><th scope='row'>{phase}</th>" + "".join(cells) + "</tr>")
        return (f'<div class="wrap"><table><thead><tr><th scope="col">Phase</th>'
                f'{heads}</tr></thead><tbody>{"".join(body)}</tbody></table></div>')

    USER_STEP = 16

    def user_buckets(self):
        """(low, high, label) population bands, USER_STEP wide, up to the peak."""
        peak = int(max(series.column_max("users_in") for series in self.series_list))
        out = []
        low = 1
        while low <= peak:
            high = low + self.USER_STEP - 1
            out.append((low, high, str(high)))
            low = high + 1
        return out

    def population_pairs(self, column):
        """[(current, reference), ...] medians per bucket, for one measure."""
        current, reference = self.series_list[0], self.series_list[1]
        return [(current.band_median(column, low, high),
                 reference.band_median(column, low, high))
                for low, high, _ in self.user_buckets()]

    def population_charts(self):
        labels = [series.label for series in self.series_list[:2]]
        buckets = [label for _, _, label in self.user_buckets()]
        charts = [
            ("Median response time", "ms", "p50_ms", 1),
            ("Requests served per second", "req/s", "calls", 0),
            ("Memory of the whole stack", "MB", "mem_mb", 0),
            ("Worker processes", "workers", "workers", 0),
        ]
        parts = []
        for title, unit, column, digits in charts:
            pairs = self.population_pairs(column)
            parts.append(BarChart(title, unit, buckets, pairs, digits).render(labels))
        return "".join(parts)

    def population_table(self):
        """The same numbers as the bars: one row per band, 'current (reference)'."""
        columns = (("p50_ms", "p50 ms", 1), ("calls", "req/s", 0),
                   ("mem_mb", "memory MB", 0), ("workers", "workers", 0))
        heads = "".join(f"<th>{html.escape(title)}</th>" for _, title, _ in columns)
        body = []
        for (low, high, _), index in zip(self.user_buckets(), range(999)):
            cells = []
            for column, _, digits in columns:
                pair = self.population_pairs(column)[index]
                def fmt(value):
                    return "—" if value is None else f"{value:.{digits}f}"
                cells.append(f"<td><span class='now'>{fmt(pair[0])}</span> "
                             f"<span class='ref'>({fmt(pair[1])})</span></td>")
            body.append(f"<tr><th scope='row'>{low}–{high}</th>" + "".join(cells) + "</tr>")
        return (f'<div class="wrap"><table class="timetable"><thead><tr>'
                f'<th scope="col">users</th>{heads}</tr></thead>'
                f'<tbody>{"".join(body)}</tbody></table></div>')

    TIME_COLUMNS = (("users_in", "users", 0), ("p50_ms", "p50 ms", 1),
                    ("calls", "req/s", 0), ("workers", "workers", 0),
                    ("mem_mb", "memory MB", 0))

    def row_near(self, series, second):
        """The series' row closest to *second*, within 3 s, or None."""
        best, distance = None, 4.0
        for row in series.rows:
            gap = abs(float(row["t"]) - second)
            if gap < distance:
                best, distance = row, gap
        return best

    def time_table(self, step=10):
        """One row every *step* seconds: each cell 'first (second)', coloured.

        The first series is the current stack (green), the second the term of
        comparison (blue, in parentheses) — the owner's reading order.
        """
        current, reference = self.series_list[0], self.series_list[1]
        span = max(float(current.rows[-1]["t"]), float(reference.rows[-1]["t"]))
        heads = "".join(f"<th>{html.escape(title)}</th>"
                        for _, title, _ in self.TIME_COLUMNS)
        body = []
        for second in range(0, int(span) + 1, step):
            row_a = self.row_near(current, second)
            row_b = self.row_near(reference, second)
            if row_a is None and row_b is None:
                continue
            cells = []
            for column, _, digits in self.TIME_COLUMNS:
                def fmt(row):
                    value = (row or {}).get(column)
                    return f"{float(value):.{digits}f}" if value not in ("", None) else "—"
                cells.append(f"<td><span class='now'>{fmt(row_a)}</span> "
                             f"<span class='ref'>({fmt(row_b)})</span></td>")
            phase = (row_a or row_b)["phase"]
            body.append(f"<tr><th scope='row'>{second}s <small>{phase}</small></th>"
                        + "".join(cells) + "</tr>")
        return (f'<div class="wrap"><table class="timetable"><thead><tr>'
                f'<th scope="col">t</th>{heads}</tr></thead>'
                f'<tbody>{"".join(body)}</tbody></table></div>')

    def merged_csv(self, path):
        """The same merge, per second, machine-readable."""
        current, reference = self.series_list[0], self.series_list[1]
        span = int(max(float(current.rows[-1]["t"]), float(reference.rows[-1]["t"])))
        with open(path, "w") as handle:
            names = [name for name, _, _ in self.TIME_COLUMNS]
            handle.write("t,phase," + ",".join(
                f"{label}_{name}" for name in names
                for label in (current.label, reference.label)) + "\n")
            for second in range(span + 1):
                row_a, row_b = self.row_near(current, second), self.row_near(reference, second)
                if row_a is None and row_b is None:
                    continue
                cells = []
                for name in names:
                    for row in (row_a, row_b):
                        cells.append(str((row or {}).get(name, "") or ""))
                handle.write(f"{second},{(row_a or row_b)['phase']}," + ",".join(cells) + "\n")

    def render(self):
        heads = "".join(f"<th>{html.escape(s.label)}</th>" for s in self.series_list)
        body = "".join(
            "<tr><th scope='row'>" + html.escape(name) + "</th>"
            + "".join(f"<td>{value}</td>" for value in values) + "</tr>"
            for name, values in self.fact_rows())
        charts = "".join(Chart(column, title, unit, self.series_list).render()
                         for column, title, unit, _ in CHARTS)
        conditions = "".join(f"<li>{html.escape(line)}</li>" for line in self.conditions)
        return f"""<title>Churn 256 — legacy vs bridge</title>
<style>
  :root {{ --ink:#1a1a1a; --dim:#5b5b5b; --rule:#d8d8d8; --bg:#ffffff; --band:#f0f4f8; }}
  :root:not([data-theme="light"]) {{}}
  @media (prefers-color-scheme: dark) {{
    :root:not([data-theme="light"]) {{ --ink:#e8e8e8; --dim:#a5a5a5; --rule:#3a3a3a;
      --bg:#151515; --band:#1e2630; }}
  }}
  :root[data-theme="dark"] {{ --ink:#e8e8e8; --dim:#a5a5a5; --rule:#3a3a3a;
    --bg:#151515; --band:#1e2630; }}
  body {{ background:var(--bg); color:var(--ink); margin:0 auto; padding:2rem 1.2rem 4rem;
    max-width:60rem; font:16px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }}
  h1 {{ font-size:1.6rem; margin:0 0 .3rem; }}
  h2 {{ font-size:1.15rem; margin:2.4rem 0 .6rem; }}
  .sub {{ color:var(--dim); margin:0 0 1.6rem; }}
  table {{ border-collapse:collapse; width:100%; font-variant-numeric:tabular-nums; }}
  th,td {{ border-bottom:1px solid var(--rule); padding:.42rem .6rem; text-align:right; }}
  th[scope=row] {{ text-align:left; font-weight:400; }}
  thead th {{ text-align:right; font-weight:600; }}
  .wrap {{ overflow-x:auto; }}
  figure {{ margin:1.6rem 0 0; }}
  figcaption {{ font-weight:600; margin-bottom:.2rem; }}
  .unit {{ font-weight:400; color:var(--dim); }}
  svg {{ width:100%; height:auto; }}
  .grid {{ stroke:var(--rule); stroke-width:1; }}
  .ylab,.xlab {{ fill:var(--dim); font-size:11px; }}
  .ylab {{ text-anchor:end; }} .xlab {{ text-anchor:middle; }}
  .legend {{ fill:var(--ink); font-size:12px; }}
  .band {{ fill:var(--band); }}
  .band-label {{ fill:var(--dim); font-size:11px; text-anchor:middle; }}
  ul {{ color:var(--dim); }} li {{ margin:.2rem 0; }}
  .missing {{ color:var(--dim); }}
  .now {{ color:#1e7d32; font-weight:600; }}
  .ref {{ color:#1565c0; }}
  :root:not([data-theme="light"]) .now {{ color:#5dbb63; }}
  :root:not([data-theme="light"]) .ref {{ color:#6ea8e0; }}
  @media (prefers-color-scheme: light) {{
    :root:not([data-theme="dark"]) .now {{ color:#1e7d32; }}
    :root:not([data-theme="dark"]) .ref {{ color:#1565c0; }}
  }}
  .timetable td {{ white-space:nowrap; }}
  .timetable small {{ color:var(--dim); font-weight:400; }}
</style>
<h1>A population of 256, climbing and draining</h1>
<p class="sub">GenroPy legacy against the genropy-asgi bridge, same site, same
database, same declared resources — one run each, sampled every second.</p>

<h2>What the run was</h2>
<ul>{conditions}</ul>

<h2>The facts</h2>
<div class="wrap"><table><thead><tr><th scope="col">&nbsp;</th>{heads}</tr></thead>
<tbody>{body}</tbody></table></div>

<h2>Latency by population</h2>
<p class="sub">Median of the per-second percentiles over every second whose
population was in that band — a hundred seconds or more each, where "the value
at the peak" would be one second of noise.</p>
{self.band_table()}

<h2>What each population costs</h2>
<p class="sub">Same bands, the resources side: the median of each measure over
the seconds the population spent in that band.</p>
{self.resource_table()}

<h2>The three phases</h2>
<p class="sub">Climbing, holding and draining are different work — a login is
expensive, a logout frees a placement, and the middle is steady state.</p>
{self.phase_table()}

<h2>By population</h2>
<p class="sub">Every measure as a function of HOW MANY USERS were logged in,
whatever the moment: the median over all the seconds spent in each band of
{self.USER_STEP} users, climb and drain together. Bars and cells read
<span class="now">bridge</span> <span class="ref">(legacy)</span>. The legacy
has no workers bar: gunicorn runs a fixed 4, declared, not measured.</p>
{self.population_charts()}
{self.population_table()}

<h2>Second by second</h2>
{charts}
"""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", action="append", required=True,
                        help="label:driver_csv:stats_csv")
    parser.add_argument("--out", required=True)
    parser.add_argument("--condition", action="append", default=[])
    parser.add_argument("--merged-csv")
    arguments = parser.parse_args()
    series_list = []
    for spec in arguments.run:
        label, driver_path, stats_path = spec.split(":", 2)
        series_list.append(Series(label, driver_path, stats_path or None))
    report = Report(series_list, arguments.condition)
    if arguments.merged_csv and len(series_list) >= 2:
        report.merged_csv(arguments.merged_csv)
        print(f"written {arguments.merged_csv}")
    page = report.render()
    with open(arguments.out, "w") as handle:
        handle.write(page)
    print(f"written {arguments.out} ({len(page)} bytes)")


if __name__ == "__main__":
    main()
