"""A complete Raft node: elections, replication, commitment, apply and snapshots.

All state transitions happen inside one asyncio event loop. Persistence calls are
synchronous and durable before any RPC response that depends on them, as required by Raft.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections import Counter, deque
from dataclasses import dataclass
from typing import Any, Literal

from backend.config import RaftSettings
from backend.raft.log import RaftLog
from backend.raft.models import (
    AppendEntriesRequest,
    AppendEntriesResponse,
    InstallSnapshotRequest,
    InstallSnapshotResponse,
    LogEntry,
    RequestVoteRequest,
    RequestVoteResponse,
)
from backend.raft.persistence import Storage
from backend.raft.rpc import RaftRPCClient, RPCError
from backend.raft.state_machine import KVStateMachine

logger = logging.getLogger(__name__)

Role = Literal["follower", "candidate", "leader"]


class NotLeaderError(RuntimeError):
    def __init__(self, leader_id: str | None = None) -> None:
        super().__init__("not the leader")
        self.leader_id = leader_id


class CommitTimeoutError(RuntimeError):
    pass


class QuorumUnavailableError(RuntimeError):
    pass


def _validate_log_command(command: Any) -> None:
    """Validate state-machine commands before they enter the replicated log.

    The controller validates HTTP requests, but a node must not trust another node (or a
    direct RPC caller) to have done the same. An unknown op could permanently wedge the
    apply loop after the entry became committed.
    """
    if not isinstance(command, dict):
        raise ValueError("command must be an object")
    op = command.get("op")
    if op not in ("set", "delete"):
        raise ValueError("command op must be set or delete")
    key = command.get("key")
    if not isinstance(key, str) or not (1 <= len(key) <= 256):
        raise ValueError("command key must be a string of 1..256 characters")
    if op == "set":
        value = command.get("value")
        if not isinstance(value, str) or len(value) > 100_000:
            raise ValueError("set value must be a string of at most 100000 characters")


@dataclass(frozen=True)
class NodeConfig:
    node_id: str
    port: int
    peers: dict[str, str]
    data_dir: str
    settings: RaftSettings
    seed: int | None = None


class RaftNode:
    def __init__(self, config: NodeConfig) -> None:
        self.config = config
        self.node_id = config.node_id
        self.settings = config.settings
        self.peers = dict(config.peers)
        self.cluster_size = len(self.peers) + 1
        self.majority = self.cluster_size // 2 + 1
        self.rng = random.Random(config.seed)

        self.storage = Storage(config.data_dir, self.node_id)
        snap_index, snap_term = self.storage.reconcile_snapshot_on_startup()
        self.log = RaftLog(self.storage.load_entries(), snap_index, snap_term)
        self.state_machine = KVStateMachine(self.storage)

        self.current_term = self.storage.current_term()
        self.voted_for = self.storage.voted_for()
        self.commit_index = self.state_machine.applied_index
        self.role: Role = "follower"
        self.leader_id: str | None = None
        self.leader_since: float | None = None
        self.last_contact = time.monotonic()
        self.election_deadline = 0.0
        self.last_quorum_time = time.monotonic()

        self.next_index: dict[str, int] = {}
        self.match_index: dict[str, int] = {}
        self.last_ack: dict[str, float] = {peer: 0.0 for peer in self.peers}

        self._rpc = RaftRPCClient(self.node_id, self.peers, self.settings.rpc_timeout)
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._pending_created: dict[int, float] = {}
        self._replication_tasks: dict[str, asyncio.Task[None]] = {}
        self._replicate_event = asyncio.Event()
        self._apply_event = asyncio.Event()
        self._running = False
        self._tasks: list[asyncio.Task[Any]] = []
        self._started_at = time.time()
        self._election_in_progress = False
        self._last_heartbeat_emit = 0.0

        self.events: deque[dict[str, Any]] = deque(maxlen=1000)
        self.event_seq = 0
        self.counters: Counter[str] = Counter()
        self.recent_commit_latencies: deque[float] = deque(maxlen=100)
        self.recent_rpc_latencies: deque[float] = deque(maxlen=100)

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        self._running = True
        self._reset_election_deadline()
        self._tasks = [
            asyncio.create_task(self._ticker_loop(), name=f"{self.node_id}-ticker"),
            asyncio.create_task(self._apply_loop(), name=f"{self.node_id}-apply"),
        ]
        self._emit("node_started", "info", f"{self.node_id} started as {self.role}")
        if self.log.last_index > 0 or self.state_machine.applied_index > 0:
            self._emit(
                "state_recovered",
                "info",
                f"recovered term={self.current_term} log={self.log.last_index} "
                f"applied={self.state_machine.applied_index}",
                term=self.current_term,
                last_log_index=self.log.last_index,
                applied_index=self.state_machine.applied_index,
            )

    async def stop(self) -> None:
        self._running = False
        self._fail_pending(NotLeaderError(None))
        self._cancel_replication_tasks()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self._rpc.close()
        self.storage.close()

    # --------------------------------------------------------------------- events
    def _emit(self, event_type: str, severity: str, message: str, **data: Any) -> None:
        self.event_seq += 1
        event = {
            "seq": self.event_seq,
            "ts": int(time.time() * 1000),
            "node_id": self.node_id,
            "type": event_type,
            "severity": severity,
            "message": message,
            "data": data,
        }
        self.events.append(event)
        log_method = logger.warning if severity in ("warning", "error", "critical") else logger.info
        log_method("[%s] %s", self.node_id, message)

    def events_since(self, after: int) -> list[dict[str, Any]]:
        return [event for event in self.events if event["seq"] > after]

    # ---------------------------------------------------------------------- timers
    def _reset_election_deadline(self) -> None:
        self.last_contact = time.monotonic()
        timeout = self.rng.uniform(
            self.settings.election_timeout_min, self.settings.election_timeout_max
        )
        self.election_deadline = time.monotonic() + timeout

    def _election_timeout_remaining_ms(self) -> int:
        if self.role == "leader":
            return 0
        return max(0, int((self.election_deadline - time.monotonic()) * 1000))

    async def _ticker_loop(self) -> None:
        while self._running:
            now = time.monotonic()
            if self.role == "leader":
                # Check-quorum extension: a leader that cannot reach a majority within an
                # election timeout stops claiming leadership. Not required by Raft safety,
                # but it prevents a minority partition from serving stale reads.
                if now - self.last_quorum_time > self.settings.election_timeout_max:
                    self._emit(
                        "leader_lease_expired",
                        "warning",
                        "leader lost contact with a majority and stepped down",
                        term=self.current_term,
                    )
                    self._step_down(self.current_term, None, reason="quorum lost")
                elif self._count_fresh_acks(now) + 1 >= self.majority:
                    self.last_quorum_time = now
            elif not self._election_in_progress and now >= self.election_deadline:
                await self._start_election()
            await asyncio.sleep(0.02)

    def _count_fresh_acks(
        self, now: float, window: float | None = None, min_ack_time: float = 0.0
    ) -> int:
        """Count peers whose last successful AppendEntries is inside ``window``.

        The default window is the election timeout because that is the check-quorum
        deadline. Read-index reads pass a much shorter window and a ``min_ack_time``:
        the acknowledgement must come from a heartbeat sent after the read began, not just
        from a recent heartbeat that may predate a competing election.
        """
        if window is None:
            window = self.settings.election_timeout_max
        return sum(
            1
            for t in self.last_ack.values()
            if t > 0 and t >= min_ack_time and now - t <= window
        )

    def _read_confirm_window(self) -> float:
        # A read waits for a heartbeat round no older than the read invocation. The window
        # bounds that round: it is deliberately shorter than the election timeout so a
        # partitioned old leader cannot keep waiting indefinitely or answer from stale acks.
        return max(0.01, self.settings.election_timeout_min / 2.0)

    # -------------------------------------------------------------------- election
    async def _start_election(self) -> None:
        if self.role == "leader" or self._election_in_progress:
            return
        self._election_in_progress = True
        self.role = "candidate"
        self.current_term += 1
        self.voted_for = self.node_id
        self.leader_id = None
        self.storage.save_term_vote(self.current_term, self.voted_for)
        self._reset_election_deadline()
        self.counters["elections_started"] += 1
        term = self.current_term
        self._emit(
            "election_started",
            "info",
            f"{self.node_id} started election for term {term}",
            term=term,
            last_log_index=self.log.last_index,
            last_log_term=self.log.last_term,
        )

        votes = {self.node_id}
        requests = [
            self._rpc.request_vote(
                peer,
                RequestVoteRequest(
                    term=term,
                    candidate_id=self.node_id,
                    last_log_index=self.log.last_index,
                    last_log_term=self.log.last_term,
                ),
            )
            for peer in self.peers
        ]
        if requests:
            self.counters["rpc_sent"] += len(requests)
            results = await asyncio.gather(*requests, return_exceptions=True)
            for peer, result in zip(self.peers, results):
                if isinstance(result, BaseException):
                    self.counters["rpc_failed"] += 1
                    self._emit(
                        "vote_request_failed",
                        "warning",
                        f"vote request to {peer} failed: {result}",
                        peer=peer,
                        error=str(result),
                    )
                    continue
                response, elapsed = result
                self._record_rpc_latency(elapsed)
                if response.term > self.current_term:
                    self._step_down(response.term, None, reason="higher term in vote response")
                    self._election_in_progress = False
                    return
                if response.vote_granted and response.term == term and self.role == "candidate":
                    votes.add(peer)
        self._election_in_progress = False
        if (
            self.role == "candidate"
            and self.current_term == term
            and len(votes) >= self.majority
        ):
            self._become_leader()
        else:
            self._reset_election_deadline()
            self._emit(
                "election_lost",
                "warning",
                f"election in term {term} failed with {len(votes)}/{self.cluster_size} votes",
                term=term,
                votes=sorted(votes),
            )

    def _become_leader(self) -> None:
        self.role = "leader"
        self.leader_id = self.node_id
        self.leader_since = time.monotonic()
        self.last_quorum_time = time.monotonic()
        self._election_in_progress = False
        self.next_index = {peer: self.log.last_index + 1 for peer in self.peers}
        self.match_index = {peer: 0 for peer in self.peers}
        self.last_ack = {peer: 0.0 for peer in self.peers}
        self.counters["elections_won"] += 1
        self._emit(
            "leader_elected",
            "info",
            f"{self.node_id} became leader for term {self.current_term}",
            term=self.current_term,
        )
        entry = self._append_local_entry(None, "noop")
        self._emit(
            "noop_appended",
            "info",
            f"leader appended no-op at index {entry.index}",
            index=entry.index,
            term=entry.term,
        )
        self._start_replication_tasks()
        self._replicate_event.set()
        self._apply_event.set()

    def _step_down(self, new_term: int, leader_id: str | None, reason: str = "") -> None:
        term_changed = new_term > self.current_term
        if term_changed:
            self.current_term = new_term
            self.voted_for = None
            self.storage.save_term_vote(self.current_term, None)
        was = self.role
        self.role = "follower"
        self.leader_id = leader_id
        self.leader_since = None
        self._cancel_replication_tasks()
        self._fail_pending(NotLeaderError(leader_id))
        self._reset_election_deadline()
        if was != "follower" or term_changed:
            self._emit(
                "role_changed",
                "warning" if was == "leader" else "info",
                f"{self.node_id} -> follower (term {self.current_term})"
                + (f": {reason}" if reason else ""),
                term=self.current_term,
                previous_role=was,
                leader_id=leader_id,
                reason=reason,
            )

    def _start_replication_tasks(self) -> None:
        self._cancel_replication_tasks()
        term = self.current_term
        for peer in self.peers:
            self._replication_tasks[peer] = asyncio.create_task(
                self._replication_loop(peer, term), name=f"{self.node_id}-replicate-{peer}"
            )

    def _cancel_replication_tasks(self) -> None:
        for task in self._replication_tasks.values():
            if not task.done():
                task.cancel()
        self._replication_tasks.clear()

    def _fail_pending(self, error: BaseException) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(error)
        self._pending.clear()
        self._pending_created.clear()

    # ---------------------------------------------------------------- RPC handlers
    def handle_request_vote(self, request: RequestVoteRequest) -> RequestVoteResponse:
        self.counters["vote_requests_rx"] += 1
        if request.term < self.current_term:
            return RequestVoteResponse(
                term=self.current_term, vote_granted=False, voter_id=self.node_id
            )
        if request.term > self.current_term:
            self._step_down(request.term, None, reason="higher term in RequestVote")

        up_to_date = (request.last_log_term, request.last_log_index) >= (
            self.log.last_term,
            self.log.last_index,
        )
        can_vote = self.voted_for in (None, request.candidate_id)
        granted = bool(up_to_date and can_vote)
        if granted:
            self.voted_for = request.candidate_id
            self.storage.save_term_vote(self.current_term, self.voted_for)
            self._reset_election_deadline()
            self.counters["votes_granted"] += 1
            self._emit(
                "vote_granted",
                "info",
                f"granted vote to {request.candidate_id} in term {request.term}",
                term=request.term,
                candidate=request.candidate_id,
            )
        else:
            self._emit(
                "vote_rejected",
                "info",
                f"rejected vote to {request.candidate_id} in term {request.term}",
                term=request.term,
                candidate=request.candidate_id,
                up_to_date=up_to_date,
                already_voted_for=self.voted_for,
            )
        return RequestVoteResponse(
            term=self.current_term, vote_granted=granted, voter_id=self.node_id
        )

    def handle_append_entries(self, request: AppendEntriesRequest) -> AppendEntriesResponse:
        self.counters["append_rx"] += 1
        if request.term < self.current_term:
            return AppendEntriesResponse(
                term=self.current_term,
                success=False,
                last_index=self.log.last_index,
                responder_id=self.node_id,
            )
        if request.term > self.current_term or self.role != "follower":
            self._step_down(
                request.term, request.leader_id, reason="valid leader AppendEntries"
            )
        self.leader_id = request.leader_id
        self._reset_election_deadline()

        if request.prev_log_index > self.log.last_index:
            return AppendEntriesResponse(
                term=self.current_term,
                success=False,
                last_index=self.log.last_index,
                responder_id=self.node_id,
                conflict_index=self.log.last_index + 1,
            )
        if request.prev_log_index < self.log.snapshot_index:
            # The entry is already covered by our snapshot. Tell the leader where we are.
            return AppendEntriesResponse(
                term=self.current_term,
                success=True,
                match_index=self.log.snapshot_index,
                last_index=self.log.last_index,
                responder_id=self.node_id,
            )
        local_prev_term = self.log.term_at(request.prev_log_index)
        if local_prev_term != request.prev_log_term:
            conflict_index = request.prev_log_index
            while (
                conflict_index - 1 > self.log.snapshot_index
                and self.log.term_at(conflict_index - 1) == local_prev_term
            ):
                conflict_index -= 1
            return AppendEntriesResponse(
                term=self.current_term,
                success=False,
                last_index=self.log.last_index,
                conflict_index=max(self.log.snapshot_index + 1, conflict_index),
                responder_id=self.node_id,
            )

        appended_entries: list[LogEntry] = []
        matched_index = request.prev_log_index
        next_expected = self.log.last_index + 1
        for raw in request.entries:
            try:
                if not isinstance(raw, dict):
                    raise ValueError("entry is not an object")
                index = raw["index"]
                term = raw["term"]
                kind = raw.get("kind", "command")
                command = raw.get("command")
                if (
                    not isinstance(index, int)
                    or isinstance(index, bool)
                    or index < 0
                    or not isinstance(term, int)
                    or isinstance(term, bool)
                    or term <= 0
                    or kind not in ("command", "noop")
                ):
                    raise ValueError("invalid entry fields")
                if kind == "command":
                    _validate_log_command(command)
                elif command is not None:
                    raise ValueError("noop entries must not carry a command")
                entry = LogEntry(index=index, term=term, kind=kind, command=command)
            except (KeyError, TypeError, ValueError) as exc:
                self._emit(
                    "append_entries_malformed",
                    "error",
                    f"rejected malformed entry from leader {request.leader_id}: {exc}",
                    leader=request.leader_id,
                )
                return AppendEntriesResponse(
                    term=self.current_term,
                    success=False,
                    last_index=self.log.last_index,
                    responder_id=self.node_id,
                )

            if entry.index <= self.log.snapshot_index:
                continue
            if entry.index != matched_index + 1:
                # Do not acknowledge a non-contiguous tail. The leader will retry from the
                # highest index we actually matched.
                break
            existing = self.log.entry_at(entry.index)
            if existing is not None:
                if existing.term == entry.term and existing.command_hash() == entry.command_hash():
                    matched_index = entry.index
                    next_expected = entry.index + 1
                    continue
                if existing.term == entry.term:
                    self._emit(
                        "log_divergence_detected",
                        "critical",
                        f"same index/term with different command at {entry.index}",
                        index=entry.index,
                        term=entry.term,
                    )
                self._truncate_at(entry.index)
                next_expected = entry.index
            if entry.index != next_expected:
                break
            appended_entries.append(entry)
            matched_index = entry.index
            next_expected += 1

        if appended_entries:
            # Persist the whole contiguous batch in one SQLite transaction. The previous
            # implementation performed one FULL-sync transaction per entry, which made large
            # AppendEntries batches needlessly expensive.
            self.storage.append_entries(appended_entries)
            for entry in appended_entries:
                self.log.append(entry)
            self.counters["log_appends"] += len(appended_entries)
            self._emit(
                "log_entries_appended",
                "info",
                f"appended {len(appended_entries)} entries from leader {request.leader_id}",
                count=len(appended_entries),
                last_log_index=self.log.last_index,
                leader=request.leader_id,
            )

        if request.leader_commit > self.commit_index:
            new_commit = min(request.leader_commit, self.log.last_index)
            if new_commit > self.commit_index:
                self.commit_index = new_commit
                self.counters["log_commits"] += 1
                self._apply_event.set()
                self._emit(
                    "commit_index_advanced",
                    "info",
                    f"commit index advanced to {new_commit}",
                    commit_index=new_commit,
                    source="leader",
                )

        return AppendEntriesResponse(
            term=self.current_term,
            success=True,
            match_index=matched_index,
            last_index=self.log.last_index,
            responder_id=self.node_id,
        )

    def _truncate_at(self, index: int) -> None:
        if index <= self.log.snapshot_index:
            raise RuntimeError("attempted to truncate compacted log")
        removed = self.log.truncate_from(index)
        self.storage.truncate_from(index)
        self.counters["log_truncations"] += 1
        self._emit(
            "log_truncated",
            "warning",
            f"truncated {len(removed)} conflicting entries from index {index}",
            index=index,
            removed=len(removed),
        )

    def handle_install_snapshot(
        self, request: InstallSnapshotRequest
    ) -> InstallSnapshotResponse:
        if request.term < self.current_term:
            return InstallSnapshotResponse(
                term=self.current_term, success=False, responder_id=self.node_id
            )
        if request.term > self.current_term or self.role != "follower":
            self._step_down(
                request.term, request.leader_id, reason="valid InstallSnapshot"
            )
        self.leader_id = request.leader_id
        self._reset_election_deadline()
        incoming_index = request.last_included_index
        try:
            payload_index, payload_term = self.storage.validate_snapshot_payload(request.data)
            if (
                payload_index != request.last_included_index
                or payload_term != request.last_included_term
            ):
                raise ValueError(
                    "snapshot payload metadata does not match the InstallSnapshot request"
                )
            if incoming_index <= self.state_machine.applied_index:
                local_term = self.log.term_at(incoming_index)
                if local_term is not None and local_term != payload_term:
                    raise ValueError(
                        "snapshot term conflicts with the locally applied entry at that index"
                    )
                return InstallSnapshotResponse(
                    term=self.current_term,
                    success=True,
                    match_index=incoming_index,
                    responder_id=self.node_id,
                )
            self.storage.install_snapshot(request.data)
        except Exception as exc:  # noqa: BLE001 - corrupt snapshot must be rejected
            self._emit(
                "snapshot_install_failed",
                "error",
                f"failed to install snapshot: {exc}",
                error=str(exc),
            )
            return InstallSnapshotResponse(
                term=self.current_term, success=False, responder_id=self.node_id
            )
        self.log = RaftLog([], payload_index, payload_term)
        self.state_machine = KVStateMachine(self.storage)
        self.commit_index = max(self.commit_index, payload_index)
        self.counters["snapshots_installed"] += 1
        self._emit(
            "snapshot_installed",
            "info",
            f"installed snapshot at index {payload_index} (term {payload_term})",
            index=payload_index,
            term=payload_term,
            leader=request.leader_id,
        )
        self._apply_event.set()
        return InstallSnapshotResponse(
            term=self.current_term,
            success=True,
            match_index=incoming_index,
            responder_id=self.node_id,
        )

    # ------------------------------------------------------------------ replication
    async def _replication_loop(self, peer: str, term: int) -> None:
        while self._running and self.role == "leader" and self.current_term == term:
            try:
                await self._replicate_once(peer)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - network errors are expected
                self.counters["rpc_failed"] += 1
                logger.debug("replication to %s failed: %s", peer, exc)
            if self.role != "leader" or self.current_term != term:
                return
            try:
                await asyncio.wait_for(
                    self._replicate_event.wait(), timeout=self.settings.heartbeat_interval
                )
            except asyncio.TimeoutError:
                pass
            self._replicate_event.clear()

    async def _replicate_once(self, peer: str) -> None:
        if self.role != "leader":
            return
        term = self.current_term
        next_i = min(self.next_index.get(peer, self.log.last_index + 1), self.log.last_index + 1)
        if next_i <= self.log.snapshot_index:
            await self._send_snapshot(peer)
            return
        prev_index = next_i - 1
        prev_term = self.log.term_at(prev_index)
        if prev_term is None:
            self.next_index[peer] = max(self.log.snapshot_index + 1, prev_index)
            return
        entries = self.log.entries_from(next_i, self.settings.max_append_entries)
        request = AppendEntriesRequest(
            term=term,
            leader_id=self.node_id,
            prev_log_index=prev_index,
            prev_log_term=prev_term,
            entries=[entry.to_dict() for entry in entries],
            leader_commit=self.commit_index,
        )
        sent_at = time.monotonic()
        self.counters["rpc_sent"] += 1
        try:
            response, elapsed = await self._rpc.append_entries(peer, request)
            self._record_rpc_latency(elapsed)
        except RPCError:
            self.counters["rpc_failed"] += 1
            return
        except Exception as exc:  # noqa: BLE001
            self.counters["rpc_failed"] += 1
            logger.debug("append_entries to %s error: %s", peer, exc)
            return

        if response.term > self.current_term:
            self._step_down(response.term, None, reason="higher term in AppendEntries response")
            return
        if self.role != "leader" or self.current_term != term:
            return

        if response.success:
            # Use the request send time, not the response receive time. A follower resets its
            # election deadline when it receives the request, so freshness starts at send.
            # Recording receive time would overstate freshness by the full RTT, which matters
            # when the simulator injects latency.
            self.last_ack[peer] = sent_at
            matched = max(self.match_index.get(peer, 0), response.match_index)
            self.match_index[peer] = matched
            self.next_index[peer] = matched + 1
            if entries:
                self.counters["append_tx"] += len(entries)
                self._emit(
                    "replication_ack",
                    "info",
                    f"{peer} acknowledged entries through {matched}",
                    peer=peer,
                    match_index=matched,
                    entries=len(entries),
                )
            elif time.monotonic() - self._last_heartbeat_emit > 2.0:
                self._last_heartbeat_emit = time.monotonic()
                self._emit(
                    "heartbeat_ack",
                    "debug",
                    f"heartbeat acknowledged by {peer}",
                    peer=peer,
                    match_index=matched,
                )
            self._advance_commit()
            if matched < self.log.last_index:
                self._replicate_event.set()
        else:
            hint = response.conflict_index or (response.last_index + 1)
            new_next = min(next_i - 1, hint)
            self.next_index[peer] = max(self.log.snapshot_index + 1, max(1, new_next))
            self._emit(
                "replication_conflict",
                "warning",
                f"log mismatch with {peer}; retrying at index {self.next_index[peer]}",
                peer=peer,
                conflict_index=self.next_index[peer],
            )
            self._replicate_event.set()

    async def _send_snapshot(self, peer: str) -> None:
        snapshot = self.storage.load_snapshot_file()
        if not snapshot:
            logger.error("no snapshot file for %s despite snapshot index", peer)
            return
        index = int(snapshot["last_included_index"])
        term = int(snapshot["last_included_term"])
        request = InstallSnapshotRequest(
            term=self.current_term,
            leader_id=self.node_id,
            last_included_index=index,
            last_included_term=term,
            data=snapshot,
        )
        sent_at = time.monotonic()
        self.counters["rpc_sent"] += 1
        try:
            response, elapsed = await self._rpc.install_snapshot(peer, request)
            self._record_rpc_latency(elapsed)
        except Exception as exc:  # noqa: BLE001
            self.counters["rpc_failed"] += 1
            logger.debug("install_snapshot to %s failed: %s", peer, exc)
            return
        if response.term > self.current_term:
            self._step_down(response.term, None, reason="higher term in InstallSnapshot response")
            return
        if response.success and self.role == "leader":
            self.match_index[peer] = max(self.match_index.get(peer, 0), index)
            self.next_index[peer] = index + 1
            self.last_ack[peer] = sent_at
            self._emit(
                "snapshot_sent",
                "info",
                f"sent snapshot at index {index} to {peer}",
                peer=peer,
                index=index,
            )
            self._advance_commit()

    def _advance_commit(self) -> None:
        if self.role != "leader":
            return
        indices = sorted([self.log.last_index, *self.match_index.values()], reverse=True)
        majority_index = indices[self.cluster_size // 2]
        if (
            majority_index > self.commit_index
            and self.log.term_at(majority_index) == self.current_term
        ):
            self.commit_index = majority_index
            self.counters["log_commits"] += 1
            self._apply_event.set()
            self._emit(
                "commit_index_advanced",
                "info",
                f"majority committed through index {majority_index}",
                commit_index=majority_index,
                term=self.current_term,
                source="leader",
            )

    # ----------------------------------------------------------------- apply loop
    def _append_local_entry(
        self, command: dict[str, Any] | None, kind: Literal["command", "noop"]
    ) -> LogEntry:
        entry = LogEntry(
            index=self.log.last_index + 1,
            term=self.current_term,
            kind=kind,
            command=command,
        )
        self.storage.append_entry(entry)
        self.log.append(entry)
        self.counters["log_appends"] += 1
        return entry

    async def _apply_loop(self) -> None:
        while self._running:
            await self._apply_event.wait()
            self._apply_event.clear()
            self._apply_committed()

    def _apply_committed(self) -> None:
        while self.state_machine.applied_index < self.commit_index:
            next_index = self.state_machine.applied_index + 1
            entry = self.log.entry_at(next_index)
            if entry is None:
                if next_index <= self.log.snapshot_index:
                    break
                logger.error("[%s] missing log entry %s for apply", self.node_id, next_index)
                break
            try:
                result = self.state_machine.apply(entry)
            except Exception as exc:  # noqa: BLE001
                self._emit(
                    "apply_failed",
                    "critical",
                    f"failed to apply index {entry.index}: {exc}",
                    index=entry.index,
                    error=str(exc),
                )
                break
            self.counters["log_applies"] += 1
            future = self._pending.pop(entry.index, None)
            created = self._pending_created.pop(entry.index, None)
            if created is not None:
                latency_ms = (time.monotonic() - created) * 1000
                self.recent_commit_latencies.append(round(latency_ms, 2))
            if future is not None and not future.done():
                future.set_result(
                    {
                        "index": entry.index,
                        "term": entry.term,
                        "result": result,
                        "leader": self.node_id,
                    }
                )
            if entry.kind == "command":
                self._emit(
                    "entry_applied",
                    "info",
                    f"applied index {entry.index} ({entry.command.get('op') if entry.command else '?'})",
                    index=entry.index,
                    term=entry.term,
                    command=entry.command,
                )
            if (
                self.settings.snapshot_threshold > 0
                and self.state_machine.applied_index - self.log.snapshot_index
                >= self.settings.snapshot_threshold
            ):
                self._create_snapshot()

    def _create_snapshot(self) -> None:
        index = self.state_machine.applied_index
        if index <= self.log.snapshot_index:
            return
        term = self.state_machine.applied_term
        kv, history = self.state_machine.snapshot()
        try:
            self.storage.create_snapshot(index, term, kv, history)
            self.log.compact_to(index, term)
        except Exception as exc:  # noqa: BLE001
            self._emit(
                "snapshot_failed",
                "error",
                f"snapshot creation failed at index {index}: {exc}",
                error=str(exc),
            )
            return
        self.counters["snapshots_created"] += 1
        self._emit(
            "snapshot_created",
            "info",
            f"compacted log through index {index}",
            index=index,
            term=term,
            remaining_entries=self.log.length,
        )

    # ------------------------------------------------------------------- clients
    async def append_command(self, command: dict[str, Any]) -> dict[str, Any]:
        if self.role != "leader":
            raise NotLeaderError(self.leader_id)
        self.counters["client_writes"] += 1
        entry = self._append_local_entry(command, "command")
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending[entry.index] = future
        self._pending_created[entry.index] = time.monotonic()
        self._replicate_event.set()
        self._emit(
            "client_write_received",
            "info",
            f"leader accepted write at index {entry.index}",
            index=entry.index,
            term=entry.term,
            command=command,
        )
        try:
            return await asyncio.wait_for(future, timeout=self.settings.client_timeout)
        except asyncio.TimeoutError as exc:
            self.counters["client_write_failures"] += 1
            self._pending.pop(entry.index, None)
            self._pending_created.pop(entry.index, None)
            self._emit(
                "client_write_timeout",
                "error",
                f"write at index {entry.index} was not committed before timeout",
                index=entry.index,
            )
            raise CommitTimeoutError(
                f"entry at index {entry.index} was not committed within "
                f"{self.settings.client_timeout}s"
            ) from exc
        except NotLeaderError:
            self.counters["client_write_failures"] += 1
            raise

    async def read_key(self, key: str) -> dict[str, Any]:
        if self.role != "leader":
            raise NotLeaderError(self.leader_id)
        self.counters["read_requests"] += 1
        term = self.current_term
        read_started = time.monotonic()
        # Do not reuse acknowledgements that predate this read. A follower may vote for a
        # higher-term candidate even while it still has a recent heartbeat from us; a fresh
        # heartbeat round after the read invocation gives us a defensible linearization
        # point before any competing leader can have committed a write we did not see.
        if not await self._confirm_quorum(since=read_started):
            raise QuorumUnavailableError("leader could not confirm a fresh majority")
        # The confirmation above may have waited for acknowledgements. It is only useful if
        # this node is still the leader in the same term. Without this check a stepped-down
        # node can return plain follower state as a successful read.
        if self.role != "leader" or self.current_term != term:
            raise NotLeaderError(self.leader_id)
        confirmed_at = time.monotonic()
        target = self.commit_index
        apply_deadline = confirmed_at + max(1.0, self._read_confirm_window())
        while self.state_machine.applied_index < target:
            if time.monotonic() > apply_deadline:
                raise QuorumUnavailableError(
                    "local state machine did not catch up to the confirmed read index"
                )
            self._apply_event.set()
            await asyncio.sleep(0.005)
        if self.role != "leader" or self.current_term != term:
            raise NotLeaderError(self.leader_id)
        value, last_index = self.state_machine.get(key)
        return {
            "key": key,
            "value": value,
            "exists": value is not None,
            "index": last_index,
            "commit_index": self.commit_index,
            "term": self.current_term,
            "read_mode": "read-index",
        }

    async def _confirm_quorum(self, since: float | None = None) -> bool:
        window = self._read_confirm_window()
        since = time.monotonic() if since is None else since
        deadline = time.monotonic() + window
        # Force the replication loops to send a round of heartbeats no older than this read.
        self._replicate_event.set()
        while time.monotonic() < deadline:
            if self.role != "leader":
                return False
            now = time.monotonic()
            fresh = 1 + self._count_fresh_acks(now, window, min_ack_time=since)
            if fresh >= self.majority:
                return True
            self._replicate_event.set()
            await asyncio.sleep(0.01)
        return False

    def kv_list(self) -> dict[str, str]:
        return self.state_machine.list()

    # -------------------------------------------------------------------- status
    def status(self) -> dict[str, Any]:
        now = time.monotonic()
        fresh = self._count_fresh_acks(now) if self.role == "leader" else 0
        rpc_avg = (
            sum(self.recent_rpc_latencies) / len(self.recent_rpc_latencies)
            if self.recent_rpc_latencies
            else 0.0
        )
        return {
            "node_id": self.node_id,
            "role": self.role,
            "term": self.current_term,
            "voted_for": self.voted_for,
            "leader_id": self.leader_id,
            "last_log_index": self.log.last_index,
            "last_log_term": self.log.last_term,
            "commit_index": self.commit_index,
            "applied_index": self.state_machine.applied_index,
            "applied_term": self.state_machine.applied_term,
            "snapshot_index": self.log.snapshot_index,
            "snapshot_term": self.log.snapshot_term,
            "log_length": self.log.length,
            "cluster_size": self.cluster_size,
            "quorum_size": self.majority,
            "peers": sorted(self.peers),
            "next_index": dict(self.next_index),
            "match_index": dict(self.match_index),
            "last_ack_age_ms": {
                peer: (None if ts == 0 else int((now - ts) * 1000))
                for peer, ts in self.last_ack.items()
            },
            "quorum_fresh": (1 + fresh) if self.role == "leader" else 0,
            "election_timeout_remaining_ms": self._election_timeout_remaining_ms(),
            "last_contact_age_ms": int((now - self.last_contact) * 1000),
            "uptime_s": round(time.time() - self._started_at, 2),
            "kv_count": self.state_machine.count(),
            "events_seq": self.event_seq,
            "counters": dict(self.counters),
            "recent_commit_latency_ms": list(self.recent_commit_latencies)[-25:],
            "avg_rpc_latency_ms": round(rpc_avg, 2),
            "persistence": self.storage.stats(),
            "running": self._running,
        }

    def log_entries(self, start: int = 1, end: int | None = None) -> dict[str, Any]:
        entries = [
            entry.to_dict()
            for entry in self.log.entries
            if entry.index >= start and (end is None or entry.index <= end)
        ]
        return {
            "node_id": self.node_id,
            "term": self.current_term,
            "role": self.role,
            "leader_id": self.leader_id,
            "snapshot_index": self.log.snapshot_index,
            "snapshot_term": self.log.snapshot_term,
            "commit_index": self.commit_index,
            "applied_index": self.state_machine.applied_index,
            "last_log_index": self.log.last_index,
            "entries": entries,
        }

    def debug_state(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "term": self.current_term,
            "role": self.role,
            "commit_index": self.commit_index,
            "applied_index": self.state_machine.applied_index,
            "snapshot_index": self.log.snapshot_index,
            "kv": self.state_machine.list(),
            "applied_history": self.storage.applied_history(500),
            "leader_id": self.leader_id,
        }

    def _record_rpc_latency(self, elapsed_ms: float) -> None:
        self.recent_rpc_latencies.append(elapsed_ms)
