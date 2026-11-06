"""Regression tests for failures found during the release audit."""

from __future__ import annotations

import asyncio
import time

import httpx
import pytest

from backend.config import RaftSettings
from backend.raft.models import AppendEntriesRequest, InstallSnapshotRequest, LogEntry
from backend.raft.node import NodeConfig, NotLeaderError, QuorumUnavailableError, RaftNode
from backend.raft.persistence import Storage
from backend.raft.state_machine import KVStateMachine

SETTINGS = RaftSettings(
    election_timeout_min=0.4,
    election_timeout_max=0.8,
    heartbeat_interval=0.1,
    rpc_timeout=0.3,
    snapshot_threshold=1000,
    client_timeout=3.0,
)


def command(index: int, term: int, key: str, value: str) -> LogEntry:
    return LogEntry(index, term, "command", {"op": "set", "key": key, "value": value})


def make_node(tmp_path) -> RaftNode:
    return RaftNode(
        NodeConfig(
            node_id="n1",
            port=9999,
            peers={"n2": "http://127.0.0.1:1", "n3": "http://127.0.0.1:2"},
            data_dir=str(tmp_path),
            settings=SETTINGS,
            seed=1,
        )
    )


def test_interrupted_install_snapshot_restores_state_and_applied_index(tmp_path) -> None:
    storage = Storage(str(tmp_path), "n1")
    storage.apply_entry(LogEntry(1, 1, "noop"))
    storage.apply_entry(command(2, 1, "k", "old"))
    storage.create_snapshot(
        2,
        1,
        {"k": "old"},
        [
            {"idx": 1, "term": 1, "command_hash": "a"},
            {"idx": 2, "term": 1, "command_hash": "b"},
        ],
    )
    payload = {
        "last_included_index": 6,
        "last_included_term": 3,
        "kv": {"k": "new", "only-in-snapshot": "yes"},
        "history": [{"idx": 6, "term": 3, "command_hash": "c"}],
    }
    # Simulate a crash after the InstallSnapshot file is durable but before the database
    # transaction that replaces KV/history/applied metadata.
    storage._write_snapshot_file(payload)
    storage.close()

    reopened = Storage(str(tmp_path), "n1")
    assert reopened.reconcile_snapshot_on_startup() == (6, 3)
    assert reopened.list_kv() == {"k": "new", "only-in-snapshot": "yes"}
    assert reopened.applied_index() == 6
    assert reopened.applied_term() == 3
    assert reopened.snapshot_index() == 6
    assert reopened.log_count() == 0
    sm = KVStateMachine(reopened)
    assert (sm.applied_index, sm.applied_term) == (6, 3)
    reopened.close()


def test_corrupt_snapshot_file_is_rebuilt_from_durable_applied_state(tmp_path) -> None:
    storage = Storage(str(tmp_path), "n1")
    entry = command(1, 1, "k", "v")
    storage.append_entry(entry)
    storage.apply_entry(entry)
    storage.create_snapshot(
        1, 1, {"k": "v"}, [{"idx": 1, "term": 1, "command_hash": entry.command_hash()}]
    )
    with open(storage.snapshot_path, "w", encoding="utf-8") as handle:
        handle.write("{not-json")
    storage.close()

    reopened = Storage(str(tmp_path), "n1")
    assert reopened.reconcile_snapshot_on_startup() == (1, 1)
    assert reopened.list_kv() == {"k": "v"}
    assert reopened.applied_index() == 1
    assert reopened.load_snapshot_file()["kv"] == {"k": "v"}
    reopened.close()


@pytest.mark.asyncio
async def test_read_confirm_rejects_acknowledgements_older_than_the_window(tmp_path) -> None:
    node = make_node(tmp_path)
    node.role = "leader"
    node.current_term = 1
    node.last_ack["n2"] = time.monotonic() - 0.5
    started = time.monotonic()
    assert await node._confirm_quorum() is False
    assert time.monotonic() - started >= SETTINGS.election_timeout_min / 2 * 0.8
    await node._rpc.close()
    node.storage.close()


@pytest.mark.asyncio
async def test_read_fails_instead_of_returning_ahead_of_applied_state(tmp_path) -> None:
    node = make_node(tmp_path)
    entry = command(1, 1, "k", "old")
    node.storage.append_entry(entry)
    node.log.append(entry)
    node.state_machine.apply(entry)
    node.role = "leader"
    node.current_term = 1
    node.commit_index = 5

    async def confirm(*, since: float | None = None) -> bool:
        return True

    node._confirm_quorum = confirm  # type: ignore[method-assign]
    started = time.monotonic()
    with pytest.raises(QuorumUnavailableError, match="catch up"):
        await node.read_key("k")
    elapsed = time.monotonic() - started
    assert 0.9 <= elapsed < 1.5
    await node._rpc.close()
    node.storage.close()


@pytest.mark.asyncio
async def test_read_rechecks_leader_after_waiting_for_quorum(tmp_path) -> None:
    node = make_node(tmp_path)
    entry = command(1, 1, "k", "old")
    node.storage.append_entry(entry)
    node.log.append(entry)
    node.state_machine.apply(entry)
    node.role = "leader"
    node.current_term = 1
    node.commit_index = 1

    async def slow_confirm(*, since: float | None = None) -> bool:
        await asyncio.sleep(0.05)
        return True

    node._confirm_quorum = slow_confirm  # type: ignore[method-assign]
    task = asyncio.create_task(node.read_key("k"))
    await asyncio.sleep(0.01)
    node.role = "follower"
    node.current_term = 2
    with pytest.raises(NotLeaderError):
        await task
    await node._rpc.close()
    node.storage.close()


@pytest.mark.asyncio
async def test_append_entries_never_acknowledges_a_gap(tmp_path) -> None:
    node = make_node(tmp_path)
    response = node.handle_append_entries(
        AppendEntriesRequest(
            term=1,
            leader_id="n2",
            prev_log_index=0,
            prev_log_term=0,
            entries=[
                {
                    "index": 2,
                    "term": 1,
                    "kind": "command",
                    "command": {"op": "set", "key": "gap", "value": "x"},
                }
            ],
            leader_commit=2,
        )
    )
    assert response.success is True
    assert response.match_index == 0
    assert node.log.last_index == 0
    assert node.storage.log_count() == 0
    await node._rpc.close()
    node.storage.close()


@pytest.mark.asyncio
async def test_install_snapshot_rejects_metadata_mismatch(tmp_path) -> None:
    node = make_node(tmp_path)
    node.current_term = 1
    payload = {
        "last_included_index": 5,
        "last_included_term": 2,
        "kv": {"k": "v"},
        "history": [],
    }
    response = node.handle_install_snapshot(
        InstallSnapshotRequest(
            term=1,
            leader_id="n2",
            last_included_index=6,
            last_included_term=2,
            data=payload,
        )
    )
    assert response.success is False
    assert node.state_machine.applied_index == 0
    assert node.storage.list_kv() == {}
    await node._rpc.close()
    node.storage.close()


@pytest.mark.asyncio
async def test_duplicate_append_entries_is_idempotent(tmp_path) -> None:
    node = make_node(tmp_path)
    entry = {
        "index": 1,
        "term": 1,
        "kind": "command",
        "command": {"op": "set", "key": "k", "value": "v"},
    }
    request = AppendEntriesRequest(
        term=1,
        leader_id="n2",
        prev_log_index=0,
        prev_log_term=0,
        entries=[entry],
        leader_commit=0,
    )
    first = node.handle_append_entries(request)
    second = node.handle_append_entries(request)
    assert first.success and second.success
    assert first.match_index == second.match_index == 1
    assert node.log.length == 1
    assert node.storage.log_count() == 1
    await node._rpc.close()
    node.storage.close()


@pytest.mark.asyncio
async def test_append_entries_rejects_unknown_command_op(tmp_path) -> None:
    node = make_node(tmp_path)
    response = node.handle_append_entries(
        AppendEntriesRequest(
            term=1,
            leader_id="n2",
            prev_log_index=0,
            prev_log_term=0,
            entries=[
                {
                    "index": 1,
                    "term": 1,
                    "kind": "command",
                    "command": {"op": "overwrite", "key": "k", "value": "v"},
                }
            ],
            leader_commit=1,
        )
    )
    assert response.success is False
    assert node.log.last_index == 0
    assert node.storage.log_count() == 0
    await node._rpc.close()
    node.storage.close()


@pytest.mark.asyncio
async def test_cors_is_opt_in_and_never_wildcard(tmp_path, monkeypatch) -> None:
    from backend.api.app import create_app
    from backend.config import ClusterSettings
    from backend.simulator.controller import ClusterController

    monkeypatch.setenv("CORS_ORIGINS", "https://console.example")
    controller = ClusterController(ClusterSettings(data_dir=str(tmp_path), auto_start=False))
    app = create_app(controller)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            preflight = {
                "Origin": "https://console.example",
                "Access-Control-Request-Method": "GET",
            }
            allowed = await client.options("/api/cluster", headers=preflight)
            assert allowed.status_code == 200
            assert allowed.headers["access-control-allow-origin"] == "https://console.example"

            denied = await client.options(
                "/api/cluster",
                headers={
                    "Origin": "https://untrusted.example",
                    "Access-Control-Request-Method": "GET",
                },
            )
            assert "access-control-allow-origin" not in denied.headers
    finally:
        await controller._client.aclose()


@pytest.mark.asyncio
async def test_cors_rejects_wildcard_at_startup(tmp_path, monkeypatch) -> None:
    from backend.api.app import create_app
    from backend.config import ClusterSettings
    from backend.simulator.controller import ClusterController

    monkeypatch.setenv("CORS_ORIGINS", "https://console.example,*")
    controller = ClusterController(
        ClusterSettings(data_dir=str(tmp_path / "wildcard"), auto_start=False)
    )
    try:
        with pytest.raises(ValueError, match="wildcard"):
            create_app(controller)
    finally:
        await controller._client.aclose()
