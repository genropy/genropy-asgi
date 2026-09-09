"""Create an offline HTML comparison from explicitly selected cycle runs.

The inputs are per-run outcome and window JSON files. No historical catalogue is
imported or changed. Reports retain failed/incomplete runs and distinguish shared
workload from environmental comparability. Latencies are per-window percentiles;
they are never averaged into a synthetic aggregate percentile.
"""

import argparse
import hashlib
import html
import json
import math
from pathlib import Path

METRICS = [
    ("offered", "Offered", ""), ("completed", "Finished calls", ""),
    ("responses_received", "HTTP responses", ""),
    ("p50_ms", "p50 latency", "ms"), ("p95_ms", "p95 latency", "ms"),
    ("p99_ms", "p99 latency", "ms"), ("late_p95_s", "p95 start delay", "s"),
    ("late_drift_s", "Start-delay drift", "s"),
    ("errors_http", "HTTP errors", ""), ("errors_app", "Application errors", ""),
    ("errors_transport", "Transport errors", ""),
    ("pending_at_end", "Pending at end", ""), ("withheld", "Withheld calls", ""),
]


def read_run(prefix):
    prefix = Path(prefix).resolve()
    files = {}
    for name in ("outcome", "windows"):
        path = Path(f"{prefix}_{name}.json")
        raw = path.read_bytes()
        files[name] = {"path": str(path), "sha256": hashlib.sha256(raw).hexdigest(),
                       "data": json.loads(raw)}
    outcome, windows = files["outcome"]["data"], files["windows"]["data"]
    if not isinstance(outcome, dict) or not isinstance(windows, list):
        raise ValueError(f"invalid outcome/windows structure: {prefix}")
    phases = [window["phase"] for window in windows]
    if len(set(phases)) != len(phases):
        raise ValueError(f"duplicate phase in {prefix}")
    return {"name": outcome.get("run") or prefix.name, "outcome": outcome,
            "windows": windows, "files": files}


def completeness(run):
    outcome = run["outcome"]
    issues = []
    if outcome.get("result") != "completa":
        issues.append(str(outcome.get("result") or "missing outcome"))
    memory = outcome.get("memory")
    if not isinstance(memory, dict) or memory.get("safety_fail") is not False:
        issues.append("memory safety not confirmed")
    if outcome.get("threads_alive"):
        issues.append("unfinished user threads")
    if not run["windows"]:
        issues.append("no windows")
    for window in run["windows"]:
        if window.get("truncated_by_stop") or window.get("pending_at_end", 0):
            issues.append(f"{window['phase']}: truncated or pending")
        if (window.get("completed") is None or window.get("offered") is None
                or window["completed"] != window["offered"]):
            issues.append(f"{window['phase']}: unfinished offered calls")
    return issues


def cell(value):
    if value is None:
        return "—"
    return html.escape(str(value))


def chart(runs, phase, key, title, unit):
    values = []
    for run in runs:
        window = next((w for w in run["windows"] if w["phase"] == phase), {})
        value = window.get(key)
        if isinstance(value, (int, float)) and math.isfinite(value) and value >= 0:
            values.append((run["name"], value))
    if not values:
        return ""
    maximum = max(value for _, value in values) or 1
    height = 35 * len(values) + 10
    bars = []
    for index, (name, value) in enumerate(values):
        y = 10 + index * 35
        bars.append(f'<text x="0" y="{y+15}">{html.escape(name)}</text>'
                    f'<rect x="240" y="{y}" width="{value/maximum*360:.2f}" height="22"/>'
                    f'<text x="610" y="{y+15}">{value:g} {unit}</text>')
    return (f'<figure><figcaption>{html.escape(title)} — {html.escape(phase)}</figcaption>'
            f'<svg role="img" aria-label="{html.escape(title)}" viewBox="0 0 760 {height}">'
            + ''.join(bars) + '</svg></figure>')


def render_report(runs):
    if not runs:
        raise ValueError("select at least one run")
    hashes = [run["outcome"].get("plan_sha256") for run in runs]
    same_plan = all(hashes) and len(set(hashes)) == 1
    plan_note = ("The selected runs declare the same request-plan hash." if same_plan else
                 "Request plans differ or a plan hash is missing: workload equivalence is unverified.")
    parts = ['<!doctype html><html lang="en"><meta charset="utf-8">'
             '<meta name="viewport" content="width=device-width, initial-scale=1">'
             '<title>Benchmark comparison</title><style>'
             'body{font:16px system-ui;max-width:1200px;margin:40px auto;padding:0 20px;color:#182535}'
             'table{border-collapse:collapse;width:100%;margin:20px 0}th,td{padding:9px;border-bottom:1px solid #ccd3dd;text-align:left}'
             'th{background:#edf2f7}td{font-variant-numeric:tabular-nums}.note{padding:16px;background:#fff3d6}'
             'svg{width:100%;max-width:900px}svg text{font:12px system-ui}svg rect{fill:#3478aa}'
             'figcaption{font-weight:600}code,li{overflow-wrap:anywhere}figure{margin:24px 0}'
             '</style><body><h1>Benchmark comparison</h1>',
             f'<p class="note">{plan_note} Matching plans alone do not establish comparable hardware, '
             'software revisions, database state or measurement definitions.</p>',
             '<table><tr><th>Run</th><th>Stack</th><th>Completion</th><th>Plan SHA-256</th></tr>']
    for run in runs:
        issues = completeness(run)
        parts.append(f'<tr><td>{cell(run["name"])}</td><td>{cell(run["outcome"].get("stack"))}</td>'
                     f'<td>{cell("; ".join(issues) if issues else "Complete")}</td>'
                     f'<td><code>{cell(run["outcome"].get("plan_sha256"))}</code></td></tr>')
    parts.append('</table><p>Finished calls include transport failures. HTTP responses are shown '
                 'separately when recorded. Missing metrics are shown as —, never as zero. '
                 'Each percentile belongs to its own window; no aggregate percentile or winner is inferred.</p>')
    phases = list(dict.fromkeys(w["phase"] for run in runs for w in run["windows"]))
    for phase in phases:
        parts.append(f'<h2>{cell(phase)}</h2><table><tr><th>Metric</th>' +
                     ''.join(f'<th>{cell(run["name"])}</th>' for run in runs) + '</tr>')
        for key, label, unit in METRICS:
            parts.append(f'<tr><th>{label}{" ("+unit+")" if unit else ""}</th>')
            for run in runs:
                window = next((w for w in run["windows"] if w["phase"] == phase), {})
                parts.append(f'<td>{cell(window.get(key))}</td>')
            parts.append('</tr>')
        parts.append('</table>')
        parts.append(chart(runs, phase, 'p95_ms', 'p95 latency', 'ms'))
        parts.append(chart(runs, phase, 'late_p95_s', 'p95 start delay', 's'))
    parts.append('<h2>Input provenance</h2><ul>')
    for run in runs:
        for record in run['files'].values():
            parts.append(f'<li>{cell(record["path"])} — <code>{record["sha256"]}</code></li>')
    parts.append('</ul></body></html>')
    return ''.join(parts)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('prefixes', nargs='+', type=Path)
    parser.add_argument('--out', required=True, type=Path)
    args = parser.parse_args()
    try:
        document = render_report([read_run(prefix) for prefix in args.prefixes])
        # Reports are artifacts too: existing output requires a new destination.
        with args.out.open('x') as stream:
            stream.write(document)
    except (ValueError, KeyError, OSError) as failure:
        parser.exit(2, f'Report failed: {failure}\n')


if __name__ == '__main__':
    main()
