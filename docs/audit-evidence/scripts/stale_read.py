import asyncio, json, tempfile, time
import httpx
from backend.config import ClusterSettings, RaftSettings
from backend.simulator.controller import ClusterController
from backend.tests.conftest import make_specs, free_port

async def main():
    tmp = tempfile.mkdtemp(prefix="raftkv-stale-")
    settings = ClusterSettings(
        cluster_size=3, data_dir=tmp, auto_start=False,
        raft=RaftSettings(election_timeout_min=0.4, election_timeout_max=4.0,
                          heartbeat_interval=0.1, rpc_timeout=0.8,
                          snapshot_threshold=64, client_timeout=5.0),
    )
    specs = make_specs(__import__('pathlib').Path(tmp), 3)
    c = ClusterController(settings, specs, seed=11)
    await c.start(wait_for_leader=0)
    try:
        leader = await c.wait_for_leader(20)
        print("initial leader", leader, flush=True)
        assert leader
        r = await c.kv_command("set", "lease", "old")
        print("baseline committed", r, flush=True)
        await c.refresh_statuses()
        st = c.node_status(leader)
        print("leader acks", st.get("last_ack_age_ms"), "term", st.get("term"), flush=True)
        old_term = st["term"]
        minority = [leader]
        majority = [n for n in c.node_ids if n != leader]
        c.set_partition([minority, majority])
        part_at = time.monotonic()
        # Wait for a new leader in the majority, then commit a new value.
        new_leader = None
        while time.monotonic() - part_at < 12:
            await c.refresh_statuses()
            candidates = [n for n in majority if (c.node_status(n) or {}).get("role") == "leader"]
            if candidates:
                new_leader = candidates[0]
                break
            await asyncio.sleep(0.05)
        print("new leader", new_leader, "after", round(time.monotonic()-part_at,3), flush=True)
        assert new_leader
        r = await c.kv_command("set", "lease", "new")
        print("new committed", r, flush=True)
        spec = next(s for s in c.specs if s.node_id == leader)
        async with httpx.AsyncClient(timeout=5.0) as client:
            # Direct old-leader read, like the controller could if its status cache is stale.
            for i in range(10):
                resp = await client.get(f"{spec.direct_url}/kv/lease")
                print("old read", i, resp.status_code, resp.text[:300], "at", round(time.monotonic()-part_at,3), flush=True)
                if resp.status_code == 200:
                    print("STALE_READ_REPRODUCED", resp.json(), flush=True)
                    return
                await asyncio.sleep(0.05)
        print("no stale read observed", flush=True)
    finally:
        await c.shutdown()

asyncio.run(main())
