"""Contracts: invalid runs remain visible and missing metrics are not invented."""

import json

import pytest

from benchmarks.analysis.cycle_report import completeness, read_run, render_report


def write_run(tmp_path, name, plan='same', result='completa', **window):
    prefix = tmp_path / name
    (tmp_path / f'{name}_outcome.json').write_text(json.dumps({
        'run': name, 'stack': name, 'plan_sha256': plan, 'result': result,
        'memory': {'safety_fail': False},
    }))
    (tmp_path / f'{name}_windows.json').write_text(json.dumps([
        dict(phase='measure', offered=10, completed=10, p95_ms=25, **window),
    ]))
    return read_run(prefix)


def test_report_preserves_invalidity_mismatched_plans_and_missing_metrics(tmp_path):
    good = write_run(tmp_path, 'legacy')
    bad = write_run(tmp_path, '<bridge>', plan='different', result='interrotta', pending_at_end=2)
    document = render_report([good, bad])
    assert completeness(good) == []
    assert completeness(bad)
    assert 'Request plans differ' in document
    assert 'interrotta' in document and 'truncated or pending' in document
    assert '&lt;bridge&gt;' in document and '<bridge>' not in document
    assert '<td>—</td>' in document
    assert 'Input provenance' in document and '<svg' in document


def test_duplicate_windows_are_rejected_instead_of_silently_selecting_one(tmp_path):
    run = write_run(tmp_path, 'legacy')
    (tmp_path / 'legacy_windows.json').write_text(json.dumps(run['windows'] * 2))
    with pytest.raises(ValueError, match='duplicate phase'):
        read_run(tmp_path / 'legacy')


def test_missing_accounting_cannot_be_reported_as_complete(tmp_path):
    run = write_run(tmp_path, 'legacy')
    del run['windows'][0]['offered']
    del run['windows'][0]['completed']
    assert completeness(run) == ['measure: unfinished offered calls']
