"""Real PostgreSQL persistence and mounted HTTP read-only contracts."""
import json
import os

import httpx
import pytest

pytest.importorskip("psycopg")

from benchmarks.portal import store
from benchmarks.portal.server import create_server

pytestmark = pytest.mark.skipif(not os.environ.get('BENCH_DATABASE_URL'),
                                reason='requires isolated portal test PostgreSQL')


@pytest.mark.asyncio
async def test_import_is_idempotent_and_portal_is_read_only(tmp_path):
    store.initialize()
    prefix = tmp_path / 'sample'
    (tmp_path / 'sample_outcome.json').write_text(json.dumps({
        'run': '<sample>', 'stack': 'bridge', 'result': 'completa',
        'memory': {'safety_fail': False}}))
    (tmp_path / 'sample_windows.json').write_text(json.dumps([
        {'phase': 'full', 'offered': 2, 'completed': 2, 'p95_ms': 5}]))
    metadata = dict(campaign=str(tmp_path), environment='synthetic-test',
                    purpose='functional', revision='test-fixture',
                    assessment={'limitations': ['Historical <unverified> metric']})
    run_id = store.import_run(prefix, **metadata)
    try:
        assert store.import_run(prefix, **metadata) == run_id
        with store.connect() as conn:
            assert conn.execute('SELECT count(*) AS n FROM benchmark_run WHERE id=%s',
                                (run_id,)).fetchone()['n'] == 1
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_server()),
                                     base_url='http://test') as client:
            assert (await client.get('/health')).status_code == 200
            assert (await client.get('/')).status_code == 200
            listing = await client.get('/benchmarks/')
            assert listing.status_code == 200 and '&lt;sample&gt;' in listing.text
            detail = await client.get('/benchmarks/' + run_id)
            assert detail.status_code == 200 and 'Complete' in detail.text
            assert 'Historical &lt;unverified&gt; metric' in detail.text
            api = await client.get('/api/benchmarks/' + run_id)
            assert api.json()['provenance']['assessment'] == metadata['assessment']
            assert api.json()['windows'][0]['completed'] == 2
            assert (await client.post('/api/benchmarks/')).status_code == 405
            assert (await client.get('/benchmarks/' + '0' * 64)).status_code == 404
    finally:
        with store.connect() as conn:
            conn.execute('DELETE FROM benchmark_run WHERE id=%s', (run_id,))
