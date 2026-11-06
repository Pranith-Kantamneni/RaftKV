from __future__ import annotations
import asyncio, tempfile, time
import httpx
from backend.api.app import create_app
from backend.simulator.controller import ClusterController
from backend.tests.conftest import make_settings, make_specs

async def main():
    tmp = tempfile.mkdtemp(prefix='raftkv-api-audit-')
    settings = make_settings(__import__('pathlib').Path(tmp))
    spec_list = make_specs(__import__('pathlib').Path(tmp))
    c = ClusterController(settings, spec_list, seed=11)
    await c.start(wait_for_leader=0)
    app = create_app(c)
    transport = httpx.ASGITransport(app=app)
    results = {}
    try:
        async with httpx.AsyncClient(transport=transport, base_url='http://audit') as client:
            leader = await c.wait_for_leader(15)
            results['leader'] = leader
            r = await client.post('/api/kv', json={'op':'set','key':'metric-key','value':'v'})
            results['write_status'] = r.status_code
            r = await client.get('/api/kv/metric-key')
            results['read'] = (r.status_code, r.json())
            r = await client.options('/api/kv/metric-key', headers={
                'Origin':'https://evil.example',
                'Access-Control-Request-Method':'GET',
            })
            results['cors_preflight_status'] = r.status_code
            results['cors_allow_origin'] = r.headers.get('access-control-allow-origin')
            long_key = 'k' * 1000
            r = await client.get('/api/kv/' + long_key)
            results['long_key_status'] = r.status_code
            r = await client.post('/api/kv', json={'op':'set','key':'k','value':'x' * 100001})
            results['oversize_value_status'] = r.status_code
            await c.refresh_statuses()
            r = await client.get('/metrics')
            text = r.text
            results['metrics_status'] = r.status_code
            results['has_counter_type'] = '# TYPE raft_client_successful_writes_total counter' in text
            results['has_failed_rpc_metric'] = 'raft_node_rpc_failed{' in text
            results['has_election_metric'] = 'raft_node_elections_started{' in text
            results['has_network_metric'] = 'raft_network_forwarded_messages' in text
            results['throughput_help_ok'] = 'includes replicated no-op entries' in text
            m = await client.get('/api/metrics')
            results['totals'] = m.json().get('totals')
            follower = next(n for n in c.node_ids if n != leader)
            await c.crash_node(follower, source='audit')
            await asyncio.sleep(1.0)
            await c.refresh_statuses()
            r = await client.get('/metrics')
            results['crashed_node'] = follower
            results['crashed_node_up_line'] = next(
                (line for line in r.text.splitlines() if line.startswith(f'raft_node_up{{node="{follower}"}}')), None
            )
            results['sample_metric_lines'] = [
                line for line in r.text.splitlines()
                if line.startswith(('raft_node_rpc_failed', 'raft_node_elections_started',
                                    'raft_network_forwarded_messages', 'raft_client_successful_writes_total'))
            ][:12]
    finally:
        await c.shutdown()
    print(__import__('json').dumps(results, indent=2, default=str))

asyncio.run(main())
