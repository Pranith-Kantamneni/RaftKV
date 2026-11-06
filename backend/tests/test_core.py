"""Unit tests for log, persistence, state machine and Raft message handling."""

from __future__ import annotations

import asyncio

import pytest

from backend.config import RaftSettings
from backend.raft.log import RaftLog
from backend.raft.models import (
    AppendEntriesRequest,
    LogEntry,
    RequestVoteRequest,
)
from backend.raft.node import NodeConfig, RaftNode
from backend.raft.persistence import Storage
from backend.raft.state_machine import KVStateMachine

SETTINGS = RaftSettings(
    election_timeout_min=0.3,
    election_timeout_max=0.5,
    heartbeat_interval=0.05,
    rpc_timeout=0.3,
    snapshot_threshold=1000,
)


def make_entry(index: int, term: int, key: str = "k", value: str | None = "v") -> LogEntry:
    return LogEntry(index=index, term=term, kind="command", command={"op": "set", "key": key, "value": value})


def test_log_term_lookup_and_truncation() -> None:
    log = RaftLog([], snapshot_index=5, snapshot_term=2)
    assert log.last_index == 5
    assert log.last_term == 2
    log.append(make_entry(6, 3))
    log.append(make_entry(7, 3))
    assert log.term_at(6) == 3
    assert log.term_at(5) == 2
    assert log.term_at(4) is None
    removed = log.truncate_from(7)
    assert [e.index for e in removed] == [7]
    assert log.last_index == 6
    log.compact_to(6, 3)
    assert log.snapshot_index == 6
    assert log.last_index == 6
    with pytest.raises(ValueError):
        log.truncate_from(6)


def test_log_requires_contiguous_entries() -> None:
    with pytest.raises(ValueError):
        RaftLog([make_entry(2, 1)], snapshot_index=0, snapshot_term=0)
    log = RaftLog()
    with pytest.raises(ValueError):
        log.append(make_entry(2, 1))


def test_storage_persists_term_vote_log_and_kv(tmp_path) -> None:
    storage = Storage(str(tmp_path), "n1")
    storage.save_term_vote(4, "n2")
    entries = [
        LogEntry(1, 1, "noop", None),
        LogEntry(2, 1, "command", {"op": "set", "key": "a", "value": "1"}),
        LogEntry(3, 4, "command", {"op": "set", "key": "b", "value": "2"}),
    ]
    storage.append_entries(entries)
    sm = KVStateMachine(storage)
    sm.apply(entries[0])
    sm.apply(entries[1])
    sm.apply(entries[2])
    history = storage.applied_history()
    storage.close()

    reopened = Storage(str(tmp_path), "n1")
    assert reopened.current_term() == 4
    assert reopened.voted_for() == "n2"
    assert [e.index for e in reopened.load_entries()] == [1, 2, 3]
    assert reopened.list_kv() == {"a": "1", "b": "2"}
    assert reopened.applied_index() == 3
    assert [h["idx"] for h in reopened.applied_history()] == [1, 2, 3]
    assert len(history) == 3
    reopened.close()


def test_state_machine_rejects_non_contiguous_apply(tmp_path) -> None:
    storage = Storage(str(tmp_path), "n1")
    sm = KVStateMachine(storage)
    with pytest.raises(RuntimeError):
        sm.apply(make_entry(2, 1))
    sm.apply(make_entry(1, 1, "x", "y"))
    assert sm.get("x") == ("y", 1)
    sm.apply(LogEntry(2, 1, "command", {"op": "delete", "key": "x"}))
    assert sm.get("x") == (None, 0)
    assert sm.applied_index == 2
    storage.close()


def test_snapshot_compaction_survives_restart(tmp_path) -> None:
    storage = Storage(str(tmp_path), "n1")
    sm = KVStateMachine(storage)
    for index in range(1, 6):
        entry = make_entry(index, 1, f"k{index}", str(index))
        storage.append_entry(entry)
        sm.apply(entry)
    kv, history = sm.snapshot()
    storage.create_snapshot(5, 1, kv, history)
    storage.delete_log_through(5)
    storage.close()

    reopened = Storage(str(tmp_path), "n1")
    reopened.reconcile_snapshot_on_startup()
    assert reopened.snapshot_index() == 5
    assert reopened.load_entries() == []
    assert reopened.list_kv()["k5"] == "5"
    reopened.close()


@pytest.mark.asyncio
async def test_vote_rules_and_persistence(tmp_path) -> None:
    config = NodeConfig(
        node_id="n1",
        port=9999,
        peers={"n2": "http://127.0.0.1:1", "n3": "http://127.0.0.1:2"},
        data_dir=str(tmp_path),
        settings=SETTINGS,
        seed=1,
    )
    node = RaftNode(config)
    try:
        granted = node.handle_request_vote(
            RequestVoteRequest(term=1, candidate_id="n2", last_log_index=0, last_log_term=0)
        )
        assert granted.vote_granted is True
        # A vote request from a stale term is now rejected.
        stale = node.handle_request_vote(
            RequestVoteRequest(term=0, candidate_id="n3", last_log_index=0, last_log_term=0)
        )
        assert stale.vote_granted is False
        assert node.current_term == 1
        assert node.voted_for == "n2"

        # Same term, another candidate: rejected; same candidate: idempotent grant.
        second = node.handle_request_vote(
            RequestVoteRequest(term=1, candidate_id="n3", last_log_index=0, last_log_term=0)
        )
        assert second.vote_granted is False
        again = node.handle_request_vote(
            RequestVoteRequest(term=1, candidate_id="n2", last_log_index=0, last_log_term=0)
        )
        assert again.vote_granted is True

        # AppendEntries from a higher term updates term and appends entries.
        response = node.handle_append_entries(
            AppendEntriesRequest(
                term=2,
                leader_id="n3",
                prev_log_index=0,
                prev_log_term=0,
                entries=[make_entry(1, 2).to_dict()],
                leader_commit=1,
            )
        )
        assert response.success is True
        assert node.current_term == 2
        assert node.voted_for is None
        assert node.log.last_index == 1
        node._apply_committed()
        assert node.state_machine.get("k")[0] == "v"

        # Stale leader append is rejected.
        stale_append = node.handle_append_entries(
            AppendEntriesRequest(
                term=1,
                leader_id="n2",
                prev_log_index=0,
                prev_log_term=0,
                entries=[],
                leader_commit=0,
            )
        )
        assert stale_append.success is False
        assert node.current_term == 2
    finally:
        await node._rpc.close()
        node.storage.close()


@pytest.mark.asyncio
async def test_append_entries_conflict_truncates_suffix(tmp_path) -> None:
    config = NodeConfig(
        node_id="n1",
        port=9999,
        peers={"n2": "http://127.0.0.1:1", "n3": "http://127.0.0.1:2"},
        data_dir=str(tmp_path),
        settings=SETTINGS,
        seed=1,
    )
    node = RaftNode(config)
    try:
        # Follower has entries from an old term at index 1 and 2.
        first = node.handle_append_entries(
            AppendEntriesRequest(
                term=1,
                leader_id="n2",
                prev_log_index=0,
                prev_log_term=0,
                entries=[make_entry(1, 1, "a", "old").to_dict(), make_entry(2, 1, "b", "old").to_dict()],
                leader_commit=0,
            )
        )
        assert first.success
        # New leader in term 2 rejects at prev=2 and sends a conflicting entry at index 2.
        conflict = node.handle_append_entries(
            AppendEntriesRequest(
                term=2,
                leader_id="n3",
                prev_log_index=1,
                prev_log_term=1,
                entries=[make_entry(2, 2, "b", "new").to_dict()],
                leader_commit=2,
            )
        )
        assert conflict.success
        assert node.log.last_index == 2
        assert node.log.term_at(2) == 2
        node._apply_committed()
        assert node.state_machine.get("b")[0] == "new"
        assert node.state_machine.get("a")[0] == "old"
    finally:
        await node._rpc.close()
        node.storage.close()


@pytest.mark.asyncio
async def test_leader_commit_requires_current_term(tmp_path) -> None:
    config = NodeConfig(
        node_id="n1",
        port=9999,
        peers={"n2": "http://127.0.0.1:1", "n3": "http://127.0.0.1:2"},
        data_dir=str(tmp_path),
        settings=SETTINGS,
        seed=1,
    )
    node = RaftNode(config)
    try:
        node.storage.save_term_vote(3, "n1")
        node.current_term = 3
        node.role = "leader"
        # Two entries from term 1, replicated to a majority, must NOT become committed
        # while the leader is in term 3 (current-term restriction).
        for index, term in [(1, 1), (2, 1)]:
            entry = LogEntry(index, term, "command", {"op": "set", "key": f"k{index}", "value": "v"})
            node.storage.append_entry(entry)
            node.log.append(entry)
        node.match_index = {"n2": 2, "n3": 0}
        node._advance_commit()
        assert node.commit_index == 0

        entry = LogEntry(3, 3, "noop", None)
        node.storage.append_entry(entry)
        node.log.append(entry)
        node.match_index = {"n2": 3, "n3": 0}
        node._advance_commit()
        assert node.commit_index == 3
    finally:
        await node._rpc.close()
        node.storage.close()
