"""Read the campaign's raw files and build the report and its charts.

Nothing is recomputed from memory: every number here comes from a file the
runs wrote, and every chart names the file it was drawn from. The output is
one self-contained HTML page — the SVG is generated here, in plain Python, so
the page opens on any machine with no library, no network and no toolchain.

    python3 analyze_campaign.py --runtime docker/runtime --out report

What it reads, when present:

- ``<prefix>_seconds.csv``  — the per-second client view: population, calls,
  percentiles, and on the bridge the pool's own columns;
- ``<prefix>_windows.csv``  — the saturation guard's judged windows;
- ``<prefix>_phases.csv``   — Prova 3's phases with the census counts;
- ``<prefix>_stats.csv``    — cpu and memory per container, from docker stats;
- ``<prefix>_procs_*.csv``  — the process table inside each container;
- ``<prefix>_hashes.txt``   — the digest of every file, carried into the page.

A missing file is not an error: the section that needed it says so and the
rest of the report is built. A campaign that stopped at a gate still produces
a page explaining where it stopped.
"""

import argparse
import csv
import html
import os


class Series:
    """One line of a chart: its name, its points, and how to say its values."""

    def __init__(self, name, points, colour, unit=""):
        self.name = name
        self.points = [(x, y) for x, y in points if y is not None]
        self.colour = colour
        self.unit = unit

    @property
    def maximum(self):
        return max((y for _, y in self.points), default=0.0)


class LineChart:
    """A plain SVG chart: axes, gridlines, one path per series, a legend."""

    WIDTH, HEIGHT = 900, 300
    LEFT, RIGHT, TOP, BOTTOM = 60, 20, 20, 40

    def __init__(self, title, series, x_label="seconds", y_label=""):
        self.title = title
        self.series = [line for line in series if line.points]
        self.x_label = x_label
        self.y_label = y_label

    @property
    def bounds(self):
        xs = [x for line in self.series for x, _ in line.points]
        ys = [y for line in self.series for _, y in line.points]
        if not xs:
            return 0, 1, 0, 1
        return min(xs), max(xs) or 1, 0, max(ys) or 1

    def place(self, x, y, x_min, x_max, y_min, y_max):
        plot_width = self.WIDTH - self.LEFT - self.RIGHT
        plot_height = self.HEIGHT - self.TOP - self.BOTTOM
        px = self.LEFT + plot_width * (x - x_min) / max(1e-9, x_max - x_min)
        py = self.TOP + plot_height * (1 - (y - y_min) / max(1e-9, y_max - y_min))
        return round(px, 1), round(py, 1)

    @property
    def svg(self):
        if not self.series:
            return f'<p class="missing">{html.escape(self.title)}: nessun dato</p>'
        x_min, x_max, y_min, y_max = self.bounds
        parts = [f'<svg viewBox="0 0 {self.WIDTH} {self.HEIGHT}" class="chart" '
                 f'role="img" aria-label="{html.escape(self.title)}">']
        for step in range(5):
            value = y_min + (y_max - y_min) * step / 4
            _, py = self.place(x_min, value, x_min, x_max, y_min, y_max)
            parts.append(f'<line x1="{self.LEFT}" y1="{py}" x2="{self.WIDTH - self.RIGHT}" '
                         f'y2="{py}" class="grid"/>')
            parts.append(f'<text x="{self.LEFT - 8}" y="{py + 4}" class="tick" '
                         f'text-anchor="end">{self.format_value(value)}</text>')
        for step in range(6):
            value = x_min + (x_max - x_min) * step / 5
            px, _ = self.place(value, y_min, x_min, x_max, y_min, y_max)
            parts.append(f'<text x="{px}" y="{self.HEIGHT - 14}" class="tick" '
                         f'text-anchor="middle">{value:.0f}</text>')
        for line in self.series:
            path = " ".join(
                ("M" if index == 0 else "L") + "{},{}".format(
                    *self.place(x, y, x_min, x_max, y_min, y_max))
                for index, (x, y) in enumerate(line.points))
            parts.append(f'<path d="{path}" fill="none" stroke="{line.colour}" '
                         f'stroke-width="1.6"/>')
        for index, line in enumerate(self.series):
            x = self.LEFT + index * 190
            parts.append(f'<rect x="{x}" y="4" width="10" height="10" fill="{line.colour}"/>')
            parts.append(f'<text x="{x + 15}" y="13" class="legend">'
                         f'{html.escape(line.name)}</text>')
        parts.append(f'<text x="{self.WIDTH / 2}" y="{self.HEIGHT - 2}" class="axis" '
                     f'text-anchor="middle">{html.escape(self.x_label)}</text>')
        parts.append("</svg>")
        return "".join(parts)

    def format_value(self, value):
        if value >= 1000:
            return f"{value / 1000:.1f}k"
        if value >= 10:
            return f"{value:.0f}"
        return f"{value:.1f}"


class CampaignReport:
    """The whole page: every prova that left files, and what it showed."""

    COLOURS = ["#2b6cb0", "#c05621", "#2f855a", "#6b46c1", "#b83280", "#4a5568"]

    def __init__(self, arguments):
        self.runtime = arguments.runtime
        self.arguments = arguments
        self.sections = []

    def path_of(self, name):
        return os.path.join(self.runtime, name)

    def read_csv(self, name):
        path = self.path_of(name)
        if not os.path.exists(path):
            return None
        with open(path) as handle:
            return list(csv.DictReader(handle))

    def numbers_from(self, rows, column):
        """The column as floats, keeping the row's elapsed second as x.

        The two files name that second differently — ``elapsed_s`` in the
        phases CSV of Prova 3, ``t`` in the per-second CSV of Prove 1 and 2 —
        so both are read. Taking only the first collapsed every per-second
        chart onto x=0.
        """
        points = []
        for row in rows or ():
            raw = (row.get(column) or "").strip()
            if not raw:
                continue
            try:
                points.append((float(row.get("elapsed_s") or row.get("t") or 0),
                               float(raw)))
            except ValueError:
                continue
        return points

    def container_series(self, prefix, column, container):
        """One container's column out of the docker stats CSV."""
        rows = self.read_csv(f"{prefix}_stats.csv")
        if not rows:
            return []
        first = None
        points = []
        for row in rows:
            if container not in (row.get("container") or ""):
                continue
            try:
                wall, value = float(row["wall"]), float(row[column])
            except (ValueError, KeyError):
                continue
            first = wall if first is None else first
            points.append((wall - first, value))
        return points

    def add_capacity_section(self, prefix, title, note):
        """Prova 1 and Prova 2 share a shape: a ramp judged in windows."""
        bridge = self.read_csv(f"{prefix}_bridge_seconds.csv")
        legacy = self.read_csv(f"{prefix}_legacy_seconds.csv")
        if not bridge and not legacy:
            self.sections.append((title, f'<p class="missing">nessun file per {prefix}.</p>'))
            return
        charts = []
        # The column names are the CSV's own: `users_in`, `p50_ms`, `p95_ms`.
        # Asked for `population` or `p50` the reader finds nothing and draws an
        # empty line, which reads exactly like a stack that did no work.
        charts.append(LineChart(
            "Popolazione e chiamate completate al secondo",
            [Series("bridge: utenti", self.numbers_from(bridge, "users_in"), self.COLOURS[0]),
             Series("bridge: calls/s", self.numbers_from(bridge, "calls"), self.COLOURS[1]),
             Series("legacy: utenti", self.numbers_from(legacy, "users_in"), self.COLOURS[2]),
             Series("legacy: calls/s", self.numbers_from(legacy, "calls"), self.COLOURS[3])]))
        charts.append(LineChart(
            "Latenze (ms) — p50 e p95, il percentile su cui la campagna decide",
            [Series("bridge p50", self.numbers_from(bridge, "p50_ms"), self.COLOURS[0]),
             Series("bridge p95", self.numbers_from(bridge, "p95_ms"), self.COLOURS[1]),
             Series("legacy p50", self.numbers_from(legacy, "p50_ms"), self.COLOURS[2]),
             Series("legacy p95", self.numbers_from(legacy, "p95_ms"), self.COLOURS[3])]))
        charts.append(LineChart(
            "CPU per container (% di un core)",
            [Series("bridge", self.container_series(prefix, "cpu_pct", "bridge"), self.COLOURS[0]),
             Series("legacy", self.container_series(prefix, "cpu_pct", "legacy"), self.COLOURS[2]),
             Series("postgres", self.container_series(prefix, "cpu_pct", "postgres"), self.COLOURS[4])]))
        charts.append(LineChart(
            "Memoria per container (MB)",
            [Series("bridge", self.container_series(prefix, "mem_mb", "bridge"), self.COLOURS[0]),
             Series("legacy", self.container_series(prefix, "mem_mb", "legacy"), self.COLOURS[2]),
             Series("postgres", self.container_series(prefix, "mem_mb", "postgres"), self.COLOURS[4])]))
        charts.append(LineChart(
            "Worker vivi (bridge)",
            [Series("worker", self.numbers_from(bridge, "workers"), self.COLOURS[0]),
             Series("utenti collocati", self.numbers_from(bridge, "users_placed"), self.COLOURS[1])]))
        body = [f"<p>{note}</p>", self.saturation_table(prefix)]
        body.extend(chart.svg for chart in charts)
        self.sections.append((title, "".join(body)))

    def saturation_table(self, prefix):
        """Where each leg crossed the thresholds, from the guard's own windows."""
        rows = ['<table><tr><th>gamba</th><th>utenti al primo p95&gt;1s</th>'
                '<th>utenti alla saturazione</th><th>motivo</th>'
                '<th>utenti al picco</th></tr>']
        for leg in ("bridge", "legacy"):
            windows = self.read_csv(f"{prefix}_{leg}_windows.csv")
            if not windows:
                rows.append(f'<tr><td>{leg}</td><td colspan="4">nessuna finestra</td></tr>')
                continue
            first_slow = next((w for w in windows
                               if w["p95_ms"] and float(w["p95_ms"]) > 1000), None)
            bad = [w for w in windows if w["bad"] == "1"]
            saturated = None
            for index in range(1, len(windows)):
                if windows[index]["bad"] == "1" and windows[index - 1]["bad"] == "1":
                    saturated = windows[index]
                    break
            peak = max((int(w["population"]) for w in windows), default=0)
            rows.append(
                f'<tr><td>{leg}</td>'
                f'<td>{first_slow["population"] if first_slow else "mai"}</td>'
                f'<td>{saturated["population"] if saturated else "mai"}</td>'
                f'<td>{html.escape(saturated["reasons"]) if saturated else ""}</td>'
                f'<td>{peak}</td></tr>')
            if not bad:
                rows.append('<tr><td colspan="5" class="note">nessuna finestra cattiva: '
                            'la gamba è finita per esaurimento della rampa, non per saturazione'
                            '</td></tr>')
        rows.append("</table>")
        return "".join(rows)

    def add_population_section(self):
        """Prova 3: the four populations, the workers, and the memory."""
        phases = self.read_csv("p3_full_bridge_phases.csv") or self.read_csv("p3_pilot_bridge_phases.csv")
        if not phases:
            self.sections.append(("Prova 3 — popolazione e freeze",
                                  '<p class="missing">nessun file di fase per la Prova 3.</p>'))
            return
        charts = [
            LineChart("Le quattro popolazioni: autenticati, residenti, collocati, congelati",
                      [Series("autenticati", self.numbers_from(phases, "authenticated"), self.COLOURS[0]),
                       Series("residenti nel driver", self.numbers_from(phases, "resident_users"), self.COLOURS[1]),
                       Series("collocati", self.numbers_from(phases, "placed"), self.COLOURS[2]),
                       Series("congelati", self.numbers_from(phases, "frozen"), self.COLOURS[3])]),
            LineChart("Worker vivi e utenti attivi",
                      [Series("worker", self.numbers_from(phases, "workers"), self.COLOURS[0]),
                       Series("attivi", self.numbers_from(phases, "active_users"), self.COLOURS[1])]),
            LineChart("Memoria del bridge durante le fasi (MB)",
                      [Series("bridge", self.container_series("p3", "mem_mb", "bridge"), self.COLOURS[0]),
                       Series("legacy", self.container_series("p3", "mem_mb", "legacy"), self.COLOURS[2])]),
        ]
        body = [self.phase_table(phases)]
        body.extend(chart.svg for chart in charts)
        self.sections.append(("Prova 3 — popolazione e freeze", "".join(body)))

    def phase_table(self, phases):
        """What each phase ended with: the numbers the PASS criteria read."""
        rows = ['<table><tr><th>fase</th><th>durata s</th><th>residenti</th>'
                '<th>congelati</th><th>collocati</th><th>worker</th></tr>']
        seen = {}
        for row in phases:
            seen.setdefault(row["phase"], []).append(row)
        for phase, entries in seen.items():
            last = entries[-1]
            span = float(last["elapsed_s"]) - float(entries[0]["elapsed_s"])
            rows.append(f'<tr><td>{html.escape(phase)}</td><td>{span:.0f}</td>'
                        f'<td>{last["resident_users"]}</td><td>{last["frozen"]}</td>'
                        f'<td>{last["placed"]}</td><td>{last["workers"]}</td></tr>')
        rows.append("</table>")
        return "".join(rows)

    def add_gates_section(self):
        """The two blocking gates, in their own words."""
        body = []
        for name, title in (("preflight_topology.txt", "Topologia e confinamento"),
                            ("driver_capacity.txt", "Capacità del generatore"),
                            ("driver_target.log", "Costo del bersaglio")):
            path = self.path_of(name)
            if os.path.exists(path):
                with open(path) as handle:
                    body.append(f"<h3>{title}</h3><pre>{html.escape(handle.read())}</pre>")
            else:
                body.append(f'<h3>{title}</h3><p class="missing">{name} assente.</p>')
        self.sections.append(("I due cancelli", "".join(body)))

    def add_provenance_section(self):
        """The digests, so the page can be tied to the bytes it was built from."""
        body = []
        for name in sorted(os.listdir(self.runtime)):
            if name.endswith("_hashes.txt"):
                with open(self.path_of(name)) as handle:
                    body.append(f"<h3>{name}</h3><pre>{html.escape(handle.read())}</pre>")
        for name in ("p3_freeze_before.txt", "p3_freeze_after.txt", "final_state.txt"):
            path = self.path_of(name)
            if os.path.exists(path):
                with open(path) as handle:
                    body.append(f"<h3>{name}</h3><pre>{html.escape(handle.read())}</pre>")
        if not body:
            body.append('<p class="missing">nessun file di provenienza.</p>')
        self.sections.append(("Provenienza", "".join(body)))

    def build(self):
        self.add_gates_section()
        self.add_capacity_section(
            "p1", "Prova 1 — capacità massima progressiva",
            "Un utente ogni 3 secondi, nessuno esce, il corpo del tour si ripete "
            "finché la guardia dichiara la saturazione. Ordine: bridge, poi legacy.")
        self.add_capacity_section(
            "p2", "Prova 2 — operatori con pause casuali",
            "Stessa rampa, ma dopo ogni operazione una pausa fra 10 e 120 secondi, "
            "presa da una traccia generata prima e identica sui due target. "
            "Ordine: legacy, poi bridge.")
        self.add_population_section()
        self.add_provenance_section()
        page = [
            "<!doctype html><html lang='it'><head><meta charset='utf-8'>",
            "<title>Campagna di capacità — genropy-asgi</title>",
            "<style>",
            "body{font:15px/1.6 system-ui,sans-serif;margin:0;background:#fbfbfd;color:#1a202c}",
            "header{background:#1a202c;color:#fff;padding:24px 32px}",
            "header h1{margin:0;font-size:22px}header p{margin:6px 0 0;opacity:.75;font-size:14px}",
            "main{max-width:1000px;margin:0 auto;padding:0 32px 64px}",
            "section{margin:32px 0;background:#fff;border:1px solid #e2e8f0;border-radius:8px;padding:20px}",
            "h2{margin:0 0 12px;font-size:18px}h3{font-size:14px;margin:18px 0 6px;color:#4a5568}",
            ".chart{width:100%;height:auto;margin:14px 0;background:#fff}",
            ".grid{stroke:#edf2f7;stroke-width:1}.tick{font:10px sans-serif;fill:#718096}",
            ".legend{font:11px sans-serif;fill:#2d3748}.axis{font:11px sans-serif;fill:#718096}",
            "table{border-collapse:collapse;width:100%;margin:12px 0;font-size:13px}",
            "th,td{border:1px solid #e2e8f0;padding:6px 10px;text-align:left}",
            "th{background:#f7fafc}.missing{color:#a0aec0;font-style:italic}",
            ".note{color:#718096;font-size:12px}",
            "pre{background:#f7fafc;border:1px solid #e2e8f0;border-radius:6px;padding:12px;",
            "overflow-x:auto;font:12px/1.5 ui-monospace,monospace}",
            "nav a{display:inline-block;margin-right:14px;color:#2b6cb0;text-decoration:none}",
            "</style></head><body>",
            "<header><h1>Campagna di capacità — genropy-asgi contro legacy</h1>",
            f"<p>generato da analyze_campaign.py su {html.escape(self.runtime)}</p></header><main>",
            "<nav>" + "".join(
                f"<a href='#s{index}'>{html.escape(title)}</a>"
                for index, (title, _) in enumerate(self.sections)) + "</nav>",
        ]
        for index, (title, body) in enumerate(self.sections):
            page.append(f"<section id='s{index}'><h2>{html.escape(title)}</h2>{body}</section>")
        page.append("</main></body></html>")
        out = f"{self.arguments.out}.html"
        with open(out, "w") as handle:
            handle.write("".join(page))
        print(f"report: {out} ({os.path.getsize(out)} byte, {len(self.sections)} sezioni)")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", default="docker/runtime")
    parser.add_argument("--out", default="charts")
    CampaignReport(parser.parse_args()).build()


if __name__ == "__main__":
    main()
