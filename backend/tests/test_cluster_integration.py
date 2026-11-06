"""Integration tests against three real Raft node processes."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from backend.tests.conftest import make_settings, make_specs
from backend.simulator.consistency import ConsistencyChecker
from backend.simulator.controller import ClusterController


async def wait_until(predicate, timeout: float = 12.0, interval: float = 0.1):
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        result = await predicate()
        if result:
            return result
        await asyncio.sleep(interval)
    return None


async def node_debug(controller, node_id: str) -> dict:
    spec = next(s for s in controller.specs if s.node_id == node_id)
    async with httpx.AsyncClient(timeout=2.0) as client:
        response = await client.get(f"{spec.direct_url}/raft/debug")
        response.raise_for_status()
        return response.json()


@pytest.mark.asyncio
async def test_election_and_replication(cluster) -> None:
    leader = await cluster.wait_for_leader(12)
    assert leader, "no leader elected"
    await cluster.refresh_statuses()
    status = cluster.node_status(leader)
    assert status
    assert status["role"] == "leader"

    result = await cluster.kv_command("set", "alpha", "1")
    assert result["committed"] is True
    index = result["index"]

    async def converged():
        statuses = {}
        for node_id in cluster.node_ids:
            debug = await node_debug(cluster, node_id)
            statuses[node_id] = debug
        return all(
            d["kv"].get("alpha") == "1" and d["applied_index"] >= index
            for d in statuses.values()
        )

    assert await wait_until(converged, timeout=10), "value did not replicate to all nodes"
    read = await cluster.kv_get("alpha")
    assert read["value"] == "1"
    assert read["exists"] is True

    # Every node's committed state agrees on the applied index/command mapping.
    histories = []
    for node_id in cluster.node_ids:
        debug = await node_debug(cluster, node_id)
        histories.append({item["idx"]: item["command_hash"] for item in debug["applied_history"]})
    common = set(histories[0])
    for history in histories[1:]:
        common &= set(history)
    for index_key in common:
        assert len({history[index_key] for history in histories}) == 1


@pytest.mark.asyncio
async def test_leader_crash_failover_and_rejoin(cluster) -> None:
    old_leader = await cluster.wait_for_leader(12)
    assert old_leader
    before = await cluster.kv_command("set", "before-failover", "yes")
    old_term = (cluster.node_status(old_leader) or {})["term"]

    await cluster.crash_node(old_leader, source="test")
    assert cluster.online[old_leader] is False

    new_leader = await cluster.wait_for_leader(15, exclude={old_leader})
    assert new_leader and new_leader != old_leader
    await cluster.refresh_statuses()
    new_term = (cluster.node_status(new_leader) or {})["term"]
    assert new_term > old_term

    after = await cluster.kv_command("set", "after-failover", "yes")
    assert after["index"] > before["index"]

    await cluster.restart_node(old_leader)

    async def old_leader_caught_up():
        if not cluster.online[old_leader]:
            return False
        debug = await node_debug(cluster, old_leader)
        return (
            debug["kv"].get("before-failover") == "yes"
            and debug["kv"].get("after-failover") == "yes"
            and debug["applied_index"] >= after["index"]
        )

    assert await wait_until(old_leader_caught_up, timeout=15), "restarted leader did not catch up"
    await cluster.refresh_statuses()
    assert (cluster.node_status(old_leader) or {})["role"] == "follower"


@pytest.mark.asyncio
async def test_partition_minority_cannot_commit(cluster) -> None:
    leader = await cluster.wait_for_leader(12)
    assert leader
    await cluster.kv_command("set", "pre-partition", "1")

    minority = [leader]
    majority = [n for n in cluster.node_ids if n != leader]
    cluster.set_partition([minority, majority])
    await asyncio.sleep(2.2)  # allow lease expiry and a majority election

    # The isolated old leader must reject writes directly (it cannot reach a majority and
    # its lease has expired). We bypass the controller here: the controller correctly
    # discovers the new majority leader for normal client traffic.
    await cluster.refresh_statuses()
    old_status = cluster.node_status(leader)
    assert old_status and old_status["role"] != "leader"
    spec = next(s for s in cluster.specs if s.node_id == leader)
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.post(
            f"{spec.direct_url}/kv",
            json={"op": "set", "key": "minority-write", "value": "forbidden"},
        )
    assert response.status_code in (409, 503, 504), response.text
    assert cluster.current_leader() != leader

    # The majority partition elects and serves writes.
    majority_leader = await cluster.wait_for_leader(12, exclude={leader})
    assert majority_leader in majority
    result = await cluster.kv_command("set", "majority-write", "allowed")
    assert result["committed"] is True

    # Heal and verify the isolated node converges without conflicting history.
    cluster.heal()
    async def converge():
        for node_id in cluster.node_ids:
            if not cluster.online.get(node_id):
                return False
            debug = await node_debug(cluster, node_id)
            if debug["kv"].get("majority-write") != "allowed":
                return False
        return True

    assert await wait_until(converge, timeout=20), "cluster did not converge after heal"
    for node_id in cluster.node_ids:
        debug = await node_debug(cluster, node_id)
        assert debug["kv"].get("minority-write") is None
    await cluster.refresh_statuses()
    assert cluster.current_leader() == majority_leader


@pytest.mark.asyncio
async def test_persistence_across_restart(cluster) -> None:
    leader = await cluster.wait_for_leader(12)
    assert leader
    result = await cluster.kv_command("set", "durable", "value")
    follower = next(n for n in cluster.node_ids if n != leader)
    await wait_until(
        lambda: _applied(cluster, follower, result["index"]), timeout=10
    )
    term_before = (cluster.node_status(follower) or {})["term"]
    await cluster.restart_node(follower)
    debug = await node_debug(cluster, follower)
    assert debug["kv"].get("durable") == "value"
    assert debug["applied_index"] >= result["index"]
    await cluster.refresh_statuses()
    status = cluster.node_status(follower)
    assert status and status["term"] >= term_before
    assert status["role"] in ("follower", "candidate", "leader")


async def _applied(controller, node_id: str, index: int) -> bool:
    debug = await node_debug(controller, node_id)
    return debug["applied_index"] >= index


@pytest.mark.asyncio
async def test_consistency_checker_on_live_cluster(cluster) -> None:
    leader = await cluster.wait_for_leader(12)
    assert leader
    checker = ConsistencyChecker(cluster)
    try:
        result = await cluster.kv_command("set", "checker:alpha", "1")
        checker.record_client_ack(result["leader"], result["index"], result["term"])
        await checker.sample()
        for index in range(4):
            await cluster.kv_command("set", f"checker:{index}", str(index))
        await asyncio.sleep(0.8)
        await checker.sample()
        convergence = await checker.check_final_convergence(timeout=10)
        report = checker.report()
    finally:
        await checker.close()

    assert convergence["converged"] is True
    assert report["passed"] is True, report["violations"]
    assert report["samples"] >= 2
    for name in ConsistencyChecker.CHECKS:
        assert report["checks"][name]["checked"] > 0, (name, report["checks"])
        assert report["checks"][name]["violations"] == 0
        assert report["checks"][name]["passed"] is True


@pytest.mark.asyncio
async def test_consistency_checker_during_leader_failure(cluster) -> None:
    """Exercise every checker invariant while the leader is killed and replaced."""
    leader = await cluster.wait_for_leader(12)
    assert leader
    checker = ConsistencyChecker(cluster)
    stop_sampler = asyncio.Event()

    async def sampler() -> None:
        while not stop_sampler.is_set():
            try:
                await checker.sample()
            except Exception:  # noqa: BLE001 - a failing sample is a test failure below
                pass
            try:
                await asyncio.wait_for(stop_sampler.wait(), timeout=0.1)
            except asyncio.TimeoutError:
                pass

    sampler_task = asyncio.create_task(sampler())
    try:
        before = await cluster.kv_command("set", "safety:before", "1")
        checker.record_client_ack(before["leader"], before["index"], before["term"])
        await asyncio.sleep(0.9)

        await cluster.crash_node(leader)
        new_leader = await cluster.wait_for_leader(15, exclude={leader})
        assert new_leader and new_leader != leader
        after = await cluster.kv_command("set", "safety:after", "2")
        checker.record_client_ack(after["leader"], after["index"], after["term"])

        await cluster.restart_node(leader)

        async def caught_up() -> bool:
            if not cluster.online.get(leader):
                return False
            debug = await node_debug(cluster, leader)
            return (
                debug["kv"].get("safety:before") == "1"
                and debug["kv"].get("safety:after") == "2"
                and debug["applied_index"] >= after["index"]
            )

        assert await wait_until(caught_up, timeout=20), "old leader did not catch up"
        await asyncio.sleep(1.2)
        convergence = await checker.check_final_convergence(timeout=10)
    finally:
        stop_sampler.set()
        await sampler_task
        await checker.close()

    report = checker.report()
    assert convergence["converged"] is True
    assert report["passed"] is True, report["violations"]
    assert report["samples"] >= 5
    for name in ConsistencyChecker.CHECKS:
        assert report["checks"][name]["checked"] > 0, (name, report["checks"])
        assert report["checks"][name]["violations"] == 0
        assert report["checks"][name]["passed"] is True


@pytest.mark.asyncio
async def test_five_node_cluster(tmp_path) -> None:
    settings = make_settings(tmp_path, size=5)
    specs = make_specs(tmp_path, size=5)
    controller = ClusterController(settings, specs, seed=21)
    await controller.start(wait_for_leader=0)
    try:
        leader = await controller.wait_for_leader(15)
        assert leader
        result = await controller.kv_command("set", "five", "nodes")
        assert result["committed"] is True
        # Crash the leader and ensure a new one appears with 4/5 nodes alive.
        await controller.crash_node(leader)
        new_leader = await controller.wait_for_leader(15, exclude={leader})
        assert new_leader and new_leader != leader
        result = await controller.kv_command("set", "five-after", "failover")
        assert result["committed"] is True
    finally:
        await controller.shutdown()


@pytest.mark.asyncio
async def test_snapshot_catchup(tmp_path) -> None:
    settings = make_settings(tmp_path, snapshot_threshold=5)
    specs = make_specs(tmp_path)
    controller = ClusterController(settings, specs, seed=31)
    await controller.start(wait_for_leader=0)
    try:
        leader = await controller.wait_for_leader(12)
        assert leader
        follower = next(n for n in controller.node_ids if n != leader)
        await controller.crash_node(follower)

        for i in range(14):
            await controller.kv_command("set", f"snap{i}", str(i))

        await controller.refresh_statuses()
        leader_status = controller.node_status(leader)
        assert leader_status and leader_status["snapshot_index"] > 0

        await controller.restart_node(follower)
        async def has_all():
            if not controller.online.get(follower):
                return False
            debug = await node_debug(controller, follower)
            return all(debug["kv"].get(f"snap{i}") == str(i) for i in range(14))
        assert await wait_until(has_all, timeout=20), "snapshot catch-up failed"
    finally:
        await controller.shutdown()
