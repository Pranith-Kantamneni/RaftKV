from __future__ import annotations

import asyncio
import json
import os
import random
import shutil
import tempfile
import time
from pathlib import Path

import httpx

from backend.config import ClusterSettings, RaftSettings
from backend.simulator.consistency import ConsistencyChecker
from backend.simulator.controller import ClusterController
from backend.tests.conftest import make_specs


async def wait_until(predicate, timeout=15.0, interval=0.1):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = await predicate()
        if value:
            return value
        await asyncio.sleep(interval)
    return None


async def debug(c, node_id):
    spec = next(s for s in c.specs if s.node_id == node_id)
    async with httpx.AsyncClient(timeout=3.0) as client:
        r = await client.get(f"{spec.direct_url}/raft/debug")
        r.raise_for_status()
        return r.json()


async def node_kv(c, node_id, key):
    return (await debug(c, node_id))["kv"].get(key)


async def all_applied(c, index, timeout=15.0):
    async def check():
        for n in c.node_ids:
            if not c.online.get(n):
                return None
            if (await debug(c, n))["applied_index"] < index:
                return None
        return True
    return await wait_until(check, timeout)


async def final_check(c, checker=None):
    await c.refresh_statuses()
    result = {"final_leader": c.current_leader(), "converged": False}
    if checker:
        result["convergence"] = await checker.check_final_convergence(15)
        result["consistency"] = checker.report()
        result["converged"] = bool(result["convergence"].get("converged"))
    return result


def make_controller(tmp: str, size: int, **overrides):
    raft = dict(
        election_timeout_min=0.4,
        election_timeout_max=0.8,
        heartbeat_interval=0.1,
        rpc_timeout=0.8,
        snapshot_threshold=64,
        client_timeout=5.0,
    )
    raft.update(overrides)
    settings = ClusterSettings(
        cluster_size=size,
        data_dir=tmp,
        auto_start=False,
        raft=RaftSettings(**raft),
    )
    specs = make_specs(Path(tmp), size)
    return ClusterController(settings, specs, seed=11)


async def with_cluster(name, size, fn, **overrides):
    tmp = tempfile.mkdtemp(prefix=f"raftkv-{name}-")
    c = make_controller(tmp, size, **overrides)
    await c.start(wait_for_leader=0)
    out = {"scenario": name, "cluster_size": size, "started": True}
    try:
        out.update(await fn(c, tmp))
        out["passed"] = not out.get("error")
    except Exception as exc:
        import traceback
        out["passed"] = False
        out["error"] = f"{type(exc).__name__}: {exc}"
        out["traceback"] = traceback.format_exc()
    finally:
        await c.shutdown()
    print(json.dumps({k: out.get(k) for k in ("scenario", "passed", "error")}, default=str), flush=True)
    return out


async def scenario_write_leader_crash(c, tmp):
    leader = await c.wait_for_leader(15)
    assert leader
    checker = ConsistencyChecker(c)
    stop = asyncio.Event()
    stats = {"success": 0, "failure": 0, "keys": [], "errors": []}

    async def sampler():
        while not stop.is_set():
            try:
                await c.refresh_statuses()
                await checker.sample()
            except Exception:
                pass
            await asyncio.sleep(0.25)

    async def writer(wid):
        seq = 0
        deadline = time.monotonic() + 9.0
        while time.monotonic() < deadline:
            seq += 1
            key = f"crash:{wid}:{seq}"
            try:
                result = await c.kv_command("set", key, f"v-{wid}-{seq}", retries=2)
                stats["success"] += 1
                stats["keys"].append((key, f"v-{wid}-{seq}", result["index"]))
                checker.record_client_ack(result["leader"], result["index"], result["term"])
            except Exception as exc:
                stats["failure"] += 1
                if len(stats["errors"]) < 10:
                    stats["errors"].append(str(exc))
            await asyncio.sleep(0.06)

    sampler_task = asyncio.create_task(sampler())
    writers = [asyncio.create_task(writer(i)) for i in range(3)]
    await asyncio.sleep(1.5)
    first_leader = c.current_leader() or leader
    crashed_at = time.monotonic()
    await c.crash_node(first_leader, source="audit")
    new_leader = await c.wait_for_leader(12, exclude={first_leader})
    crash_election_s = round(time.monotonic() - crashed_at, 3)
    await asyncio.sleep(1.5)
    restarted_at = time.monotonic()
    await c.restart_node(first_leader)
    await asyncio.gather(*writers)
    stop.set()
    await sampler_task
    await c.refresh_statuses()
    await asyncio.sleep(1.0)
    convergence = await checker.check_final_convergence(20)
    report = checker.report()
    await checker.close()
    missing = []
    for key, value, index in stats["keys"]:
        found = await node_kv(c, new_leader, key) if new_leader else None
        if found != value:
            missing.append(key)
    return {
        "first_leader": first_leader,
        "new_leader": new_leader,
        "crash_election_s": crash_election_s,
        "rejoin_s": round(time.monotonic() - restarted_at, 3),
        "writes_success": stats["success"],
        "writes_failure": stats["failure"],
        "errors": stats["errors"],
        "missing_successful_keys": missing[:10],
        "converged": convergence.get("converged"),
        "consistency_passed": report["passed"],
        "consistency_violations": report["total_violations"],
        "checks": report["checks"],
        "error": None if (not missing and report["passed"] and convergence.get("converged")) else "safety/convergence failure",
    }


async def scenario_crash_after_commit(c, tmp):
    leader = await c.wait_for_leader(15)
    assert leader
    result = await c.kv_command("set", "after-ack", "durable")
    await c.crash_node(result["leader"], source="audit")
    new_leader = await c.wait_for_leader(12, exclude={result["leader"]})
    assert new_leader
    value = await wait_until(lambda: node_kv(c, new_leader, "after-ack"), 12)
    await c.restart_node(result["leader"])
    converged = await all_applied(c, result["index"], 20)
    return {
        "committed_index": result["index"],
        "old_leader": result["leader"],
        "new_leader": new_leader,
        "value_on_new_leader": value,
        "converged": bool(converged),
        "error": None if value == "durable" and converged else "acknowledged write lost",
    }


async def scenario_follower_recovery(c, tmp):
    leader = await c.wait_for_leader(15)
    assert leader
    follower = next(n for n in c.node_ids if n != leader)
    base = await c.kv_command("set", "follower:base", "1")
    await c.crash_node(follower, source="audit")
    during = await c.kv_command("set", "follower:during", "2")
    assert during["committed"]
    await c.restart_node(follower)
    converged = await all_applied(c, during["index"], 20)
    value = await node_kv(c, follower, "follower:during")
    return {
        "leader": leader,
        "follower": follower,
        "base_index": base["index"],
        "during_index": during["index"],
        "caught_up": bool(converged),
        "value_on_recovered_follower": value,
        "error": None if value == "2" and converged else "follower did not recover",
    }


async def scenario_partition_minority_3(c, tmp):
    leader = await c.wait_for_leader(15)
    assert leader
    await c.kv_command("set", "partition:base", "1")
    majority = [n for n in c.node_ids if n != leader]
    c.set_partition([[leader], majority])
    new_leader = await c.wait_for_leader(12, exclude={leader})
    assert new_leader and new_leader in majority
    committed = await c.kv_command("set", "partition:majority", "2")
    spec = next(s for s in c.specs if s.node_id == leader)
    direct_status = None
    direct_text = ""
    try:
        async with httpx.AsyncClient(timeout=12.0) as client:
            r = await client.post(f"{spec.direct_url}/kv", json={"op": "set", "key": "partition:forbidden", "value": "x"})
            direct_status = r.status_code
            direct_text = r.text[:200]
    except httpx.HTTPError as exc:
        direct_status = f"transport:{type(exc).__name__}"
        direct_text = str(exc)
    # A direct read of the old leader is a lease probe; after a new-leader commit it must not
    # return the pre-partition value.
    old_read_status = None
    old_read_value = None
    try:
        async with httpx.AsyncClient(timeout=12.0) as client:
            r = await client.get(f"{spec.direct_url}/kv/partition:base")
            old_read_status = r.status_code
            if r.status_code == 200:
                old_read_value = r.json().get("value")
    except httpx.HTTPError as exc:
        old_read_status = f"transport:{type(exc).__name__}"
    c.heal()
    converged = await all_applied(c, committed["index"], 20)
    values = {n: await node_kv(c, n, "partition:majority") for n in c.node_ids}
    forbidden = {n: await node_kv(c, n, "partition:forbidden") for n in c.node_ids}
    return {
        "old_leader": leader,
        "new_leader": new_leader,
        "new_committed_index": committed["index"],
        "minority_direct_write_status": direct_status,
        "minority_direct_write_body": direct_text,
        "old_leader_read_status": old_read_status,
        "old_leader_read_value": old_read_value,
        "minority_write_rejected": direct_status in (409, 503, 504) or str(direct_status).startswith("transport"),
        "stale_read_observed": old_read_status == 200 and old_read_value == "1",
        "converged": bool(converged),
        "values": values,
        "forbidden_values": forbidden,
        "error": None if (direct_status in (409, 503, 504) and converged and all(v == "2" for v in values.values()) and all(v is None for v in forbidden.values())) else "partition safety failure",
    }


async def scenario_partition_leader_minority_5(c, tmp):
    leader = await c.wait_for_leader(20)
    assert leader
    follower = next(n for n in c.node_ids if n != leader)
    minority = [leader, follower]
    majority = [n for n in c.node_ids if n not in minority]
    await c.kv_command("set", "p5:base", "1")
    c.set_partition([minority, majority])
    new_leader = await c.wait_for_leader(15, exclude=set(minority))
    assert new_leader and new_leader in majority
    committed = await c.kv_command("set", "p5:majority", "2")
    spec = next(s for s in c.specs if s.node_id == leader)
    status = None
    try:
        async with httpx.AsyncClient(timeout=12.0) as client:
            r = await client.post(f"{spec.direct_url}/kv", json={"op": "set", "key": "p5:forbidden", "value": "x"})
            status = r.status_code
    except httpx.HTTPError as exc:
        status = f"transport:{type(exc).__name__}"
    c.heal()
    converged = await all_applied(c, committed["index"], 25)
    values = {n: await node_kv(c, n, "p5:majority") for n in c.node_ids}
    return {
        "leader": leader,
        "minority": minority,
        "majority": majority,
        "new_leader": new_leader,
        "minority_write_status": status,
        "minority_write_rejected": status in (409, 503, 504) or str(status).startswith("transport"),
        "converged": bool(converged),
        "values": values,
        "error": None if (status in (409, 503, 504) and converged and all(v == "2" for v in values.values())) else "5-node partition failure",
    }


async def scenario_two_nodes_isolated(c, tmp):
    leader = await c.wait_for_leader(20)
    assert leader
    # Isolate two nodes. If the leader is among them, the remaining three must elect a new leader.
    isolated = [n for n in c.node_ids if n != leader][:2]
    group = [leader, isolated[0]]
    other = [n for n in c.node_ids if n not in group]
    # Use a 2-vs-3 split; group includes leader, so the 3-node side is the majority.
    await c.kv_command("set", "two:base", "1")
    c.set_partition([group, other])
    majority_leader = await c.wait_for_leader(15, exclude=set(group))
    assert majority_leader and majority_leader in other
    committed = await c.kv_command("set", "two:majority", "2")
    c.heal()
    converged = await all_applied(c, committed["index"], 25)
    values = {n: await node_kv(c, n, "two:majority") for n in c.node_ids}
    return {
        "isolated_group": group,
        "majority_group": other,
        "new_leader": majority_leader,
        "converged": bool(converged),
        "values": values,
        "error": None if converged and all(v == "2" for v in values.values()) else "isolated nodes did not reconcile",
    }


async def scenario_rapid_leader_failures(c, tmp):
    leader = await c.wait_for_leader(20)
    assert leader
    await c.kv_command("set", "rapid:0", "0")
    crashed = []
    committed = []
    current = leader
    for i in range(2):
        crashed.append(current)
        await c.crash_node(current, source="audit")
        current = await c.wait_for_leader(15, exclude=set(crashed))
        assert current and current not in crashed
        result = await c.kv_command("set", f"rapid:{i+1}", str(i+1))
        committed.append(result["index"])
    final_leader = current
    await c.refresh_statuses()
    survivors = [n for n in c.node_ids if n not in crashed]
    value_map = {}
    for n in survivors:
        value_map[n] = await node_kv(c, n, "rapid:2")
    for old in crashed:
        await c.restart_node(old)
    converged = await all_applied(c, committed[-1], 25)
    all_values = {n: await node_kv(c, n, "rapid:2") for n in c.node_ids}
    return {
        "initial_leader": leader,
        "crashed": crashed,
        "final_leader": final_leader,
        "committed_indexes": committed,
        "survivor_values": value_map,
        "converged": bool(converged),
        "all_values": all_values,
        "error": None if converged and all(v == "2" for v in all_values.values()) else "rapid failures lost data",
    }


async def scenario_restart_all(c, tmp):
    leader = await c.wait_for_leader(15)
    assert leader
    result = await c.kv_command("set", "restart:all", "present")
    await c.supervisor.stop_all()
    await asyncio.sleep(0.5)
    await c.supervisor.start_all()
    new_leader = await c.wait_for_leader(20)
    assert new_leader
    await c.refresh_statuses()
    read = await c.kv_get("restart:all")
    converged = await all_applied(c, result["index"], 20)
    return {
        "old_leader": leader,
        "new_leader": new_leader,
        "committed_index": result["index"],
        "read": read,
        "converged": bool(converged),
        "error": None if read.get("value") == "present" and converged else "restart-all lost state",
    }


async def scenario_stale_persisted_state(c, tmp):
    leader = await c.wait_for_leader(15)
    assert leader
    victim = next(n for n in c.node_ids if n != leader)
    base = await c.kv_command("set", "stale:before", "old")
    await wait_until(lambda: all_applied(c, base["index"], 8), 10)
    spec = next(s for s in c.specs if s.node_id == victim)
    backup = os.path.join(tmp, "stale-backup")
    await c.crash_node(victim, source="audit")
    shutil.copytree(spec.data_dir, backup)
    during = await c.kv_command("set", "stale:during", "new")
    assert during["committed"]
    # Replace the stopped node's durable state with the older backup.
    shutil.rmtree(spec.data_dir)
    shutil.copytree(backup, spec.data_dir)
    await c.restart_node(victim)
    converged = await all_applied(c, during["index"], 25)
    before = await node_kv(c, victim, "stale:before")
    during_value = await node_kv(c, victim, "stale:during")
    return {
        "leader": leader,
        "victim": victim,
        "base_index": base["index"],
        "during_index": during["index"],
        "converged": bool(converged),
        "before_value": before,
        "during_value": during_value,
        "error": None if converged and before == "old" and during_value == "new" else "stale state did not reconcile",
    }


def check_linearizable(writes, reads, initial="init"):
    writes = sorted(writes, key=lambda w: w["index"])
    for i, left in enumerate(writes):
        for right in writes[i + 1:]:
            if right["end"] <= left["start"]:
                return False, f"commit order violates real time: {left} before {right}"
    for read in reads:
        valid = []
        for k in range(len(writes) + 1):
            value = initial if k == 0 else writes[k - 1]["value"]
            if value != read["value"]:
                continue
            if any(w["end"] <= read["start"] for w in writes[k:]):
                continue
            if any(w["start"] >= read["end"] for w in writes[:k]):
                continue
            valid.append(k)
        if not valid:
            return False, f"read {read!r} has no valid cut; writes={writes!r}"
    return True, "linearizable under the committed write order"


async def scenario_linearizability(c, tmp):
    leader = await c.wait_for_leader(15)
    assert leader
    await c.kv_command("set", "lin", "init")
    writes = []
    reads = []
    errors = []

    async def writer(wid):
        for seq in range(8):
            await asyncio.sleep(0.012 + 0.004 * wid)
            start = time.monotonic()
            value = f"w{wid}-{seq}"
            try:
                result = await c.kv_command("set", "lin", value, retries=0)
                end = time.monotonic()
                writes.append({"index": result["index"], "value": value, "start": start, "end": end, "writer": wid, "seq": seq})
            except Exception as exc:
                errors.append(f"writer {wid}: {exc}")

    async def reader(rid):
        for seq in range(12):
            await asyncio.sleep(0.007 + 0.002 * rid)
            start = time.monotonic()
            try:
                result = await c.kv_get("lin")
                end = time.monotonic()
                reads.append({"value": result.get("value"), "start": start, "end": end, "reader": rid, "seq": seq})
            except Exception as exc:
                errors.append(f"reader {rid}: {exc}")

    await asyncio.gather(*(writer(i) for i in range(2)), *(reader(i) for i in range(3)))
    ok, detail = check_linearizable(writes, reads)
    return {
        "writes": len(writes),
        "reads": len(reads),
        "errors": errors[:5],
        "linearizable": ok,
        "detail": detail,
        "error": None if ok and not errors else "linearizability failure",
    }


SCENARIOS = [
    ("write_leader_crash", 3, scenario_write_leader_crash),
    ("crash_after_commit", 3, scenario_crash_after_commit),
    ("follower_recovery", 3, scenario_follower_recovery),
    ("partition_minority_3", 3, scenario_partition_minority_3),
    ("partition_leader_minority_5", 5, scenario_partition_leader_minority_5),
    ("two_nodes_isolated", 5, scenario_two_nodes_isolated),
    ("rapid_leader_failures", 5, scenario_rapid_leader_failures),
    ("restart_all", 3, scenario_restart_all),
    ("stale_persisted_state", 3, scenario_stale_persisted_state),
    ("linearizability", 3, scenario_linearizability),
]


async def main():
    results = []
    only = os.getenv("AUDIT_ONLY")
    for name, size, fn in SCENARIOS:
        if only and only != name:
            continue
        results.append(await with_cluster(name, size, fn))
    out = {"results": results, "passed": all(r.get("passed") for r in results)}
    print(json.dumps(out, indent=2, default=str))
    return out


if __name__ == "__main__":
    r = asyncio.run(main())
    raise SystemExit(0 if r["passed"] else 1)
