# Copyright 2025-2026 Softwell S.r.l.
# Licensed under the Apache License, Version 2.0
"""Explicit, immutable import of selected population-cycle results."""
import argparse
import hashlib
import json
import os

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from benchmarks.analysis.cycle_report import read_run

SCHEMA = """
CREATE TABLE IF NOT EXISTS benchmark_run (
    id text PRIMARY KEY,
    imported_at timestamptz NOT NULL DEFAULT now(),
    campaign text NOT NULL,
    environment text NOT NULL,
    purpose text NOT NULL CHECK (purpose IN ('functional', 'performance')),
    revision text NOT NULL,
    run_name text NOT NULL,
    stack text NOT NULL,
    outcome jsonb NOT NULL,
    windows jsonb NOT NULL,
    provenance jsonb NOT NULL
);
"""


def connect():
    return psycopg.connect(os.environ['BENCH_DATABASE_URL'], row_factory=dict_row,
                          connect_timeout=5, options='-c statement_timeout=5000')


def initialize():
    with connect() as conn:
        conn.execute(SCHEMA)


def import_run(prefix, *, campaign, environment, purpose, revision, assessment=None):
    if not all((campaign, environment, revision)) or purpose not in ('functional', 'performance'):
        raise ValueError('campaign, environment, purpose and revision are required')
    run = read_run(prefix)
    provenance = {key: {'sha256': value['sha256']} for key, value in run['files'].items()}
    if assessment is not None:
        if not isinstance(assessment, dict):
            raise ValueError('assessment must be a JSON object')
        provenance['assessment'] = assessment
    identity = dict(campaign=campaign, environment=environment, purpose=purpose,
                    revision=revision, provenance=provenance)
    digest = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    with connect() as conn:
        conn.execute('''INSERT INTO benchmark_run
            (id, campaign, environment, purpose, revision, run_name, stack,
             outcome, windows, provenance) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (id) DO NOTHING''',
            (digest, campaign, environment, purpose, revision, run['name'],
             run['outcome'].get('stack', 'unknown'), Jsonb(run['outcome']),
             Jsonb(run['windows']), Jsonb(provenance)))
    return digest


def list_runs():
    with connect() as conn:
        return conn.execute('''SELECT id, imported_at::text, campaign, environment,
            purpose, revision, run_name, stack, outcome->>'result' AS result
            FROM benchmark_run ORDER BY imported_at DESC, id LIMIT 200''').fetchall()


def get_run(run_id):
    with connect() as conn:
        return conn.execute('''SELECT id, campaign, environment, purpose, revision,
            run_name, stack, outcome, windows, provenance FROM benchmark_run
            WHERE id=%s''', (run_id,)).fetchone()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('init')
    imp = sub.add_parser('import')
    imp.add_argument('prefix')
    for name in ('campaign', 'environment', 'revision'):
        imp.add_argument('--' + name, required=True)
    imp.add_argument('--purpose', choices=['functional', 'performance'], required=True)
    imp.add_argument('--assessment', help='Public, secret-free JSON assessment and artifact references')
    args = vars(parser.parse_args())
    if args.pop('command') == 'init':
        initialize()
    else:
        if args.get('assessment'):
            from pathlib import Path
            args['assessment'] = json.loads(Path(args['assessment']).read_text())
        print(import_run(**args))


if __name__ == '__main__':
    main()
