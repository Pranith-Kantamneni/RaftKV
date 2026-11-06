"""Cluster controller: lifecycle, observability, client gateway and fault controls.

The controller is deliberately *not* part of consensus. It observes node status and forwards
operations to the discovered leader. If it disappears, Raft continues to make progress.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import statistics
import time
from collections import deque
from typing import Any

import httpx

from backend.config import ClusterSettings, NodeSpec, RaftSettings
from backend.simulator.network import MeshProxy, NetworkSimulator
from backend.simulator.processes import ClusterSupervisor, NodeProcessError

logger = logging.getLogger(__name__)


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return round(ordered[0], 2)
    k = (len(ordered) - 1) * (p / 100.0)
    lower = int(k)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = k - lower
    result = ordered[lower] + (ordered[upper] - ordered[lower]) * fraction
    return round(result, 2)


class ClusterController:
    def __init__(
        self,
        settings: ClusterSettings | None = None,
        specs: list[NodeSpec] | None = None,
        env_overrides: dict[str, str] | None = None,
        seed: int = 7,
    ) -> None:
        self.settings = settings or ClusterSettings.from_env()
        self.specs = specs or self.settings.build_nodes()
        self.node_ids = [spec.node_id for spec in self.specs]
        self.env_overrides = dict(env_overrides or {})
        self.seed = seed
        self.network = NetworkSimulator(self.node_ids, seed=seed)
        self.network.add_listener(self._handle_network_event)
        self.proxies: dict[str, MeshProxy] = {}
        self.proxy_urls: dict[str, str] = {
            spec.node_id: spec.proxy_url for spec in self.specs
        }
        self.supervisor: ClusterSupervisor | None = None
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(9.0, connect=2.0),
            limits=httpx.Limits(max_connections=64, max_keepalive_connections=16),
        )
        self._running = False
        self._tasks: list[asyncio.Task[Any]] = []
        self._started_at: float | None = None
        self.online: dict[str, bool] = {node_id: False for node_id in self.node_ids}
        self.statuses: dict[str, dict[str, Any] | None] = {
            node_id: None for node_id in self.node_ids
        }
        self.last_seen: dict[str, float] = {node_id: 0.0 for node_id in self.node_ids}
        self._node_event_seq: dict[str, int] = {node_id: 0 for node_id in self.node_ids}
        self.events: deque[dict[str, Any]] = deque(maxlen=5000)
        self._event_seq = 0
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self._event_log = None
        self._event_log_path = os.path.join(self.settings.data_dir, "events.jsonl")
        self.metrics_history: deque[dict[str, Any]] = deque(maxlen=360)
        self._latencies: list[float] = []
        self._read_latencies: list[float] = []
        self._network_latencies: deque[float] = deque(maxlen=300)
        self._interval_success = 0
        self._interval_failure = 0
        self._total_success = 0
        self._total_failure = 0
        self._last_commit_sample: tuple[float, int, str | None] = (0.0, 0, None)
        self._election_started: dict[int, float] = {}
        self._election_durations: deque[int] = deque(maxlen=50)
        self.last_election_ms: int | None = None
        self._inflight: dict[str, dict[str, Any]] = {}
        self._random_task: asyncio.Task[Any] | None = None
        self._random_recovery_tasks: set[asyncio.Task[Any]] = set()
        self._random_crashed: set[str] = set()
        self.random_state: dict[str, Any] = {
            "running": False,
            "config": None,
            "failures": 0,
            "recoveries": 0,
        }
        self._experiment_lock = asyncio.Lock()
        self._experiments: dict[str, dict[str, Any]] = {}
        self._experiments_dir = os.path.join(self.settings.data_dir, "experiments")
        os.makedirs(self._experiments_dir, exist_ok=True)
        self._load_experiment_index()

    # --------------------------------------------------------------- properties
    @property
    def running(self) -> bool:
        return self._running

    def current_leader_status(self) -> dict[str, Any] | None:
        leaders = [
            status
            for node_id, status in self.statuses.items()
            if status and self.online.get(node_id) and status.get("role") == "leader"
        ]
        if not leaders:
            return None
        return sorted(leaders, key=lambda s: s.get("term", 0), reverse=True)[0]

    def current_leader(self) -> str | None:
        status = self.current_leader_status()
        return status["node_id"] if status else None

    def node_status(self, node_id: str) -> dict[str, Any] | None:
        return self.statuses.get(node_id)

    # ------------------------------------------------------------------ events
    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=2000)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self._subscribers.discard(queue)

    def record_event(
        self,
        event_type: str,
        message: str,
        *,
        severity: str = "info",
        source: str = "controller",
        node_id: str | None = None,
        data: dict[str, Any] | None = None,
        ts: int | None = None,
    ) -> dict[str, Any]:
        self._event_seq += 1
        event = {
            "seq": self._event_seq,
            "ts": ts or int(time.time() * 1000),
            "source": source,
            "node_id": node_id,
            "type": event_type,
            "severity": severity,
            "message": message,
            "data": data or {},
        }
        self.events.append(event)
        if self._event_log:
            try:
                self._event_log.write(json.dumps(event) + "\n")
                self._event_log.flush()
            except (OSError, ValueError):
                pass
        self._broadcast({"type": "event", "event": event})
        self._track_election_event(event)
        return event

    def _track_election_event(self, event: dict[str, Any]) -> None:
        if event.get("source") != "node":
            return
        data = event.get("data") or {}
        term = data.get("term")
        if event["type"] == "election_started" and isinstance(term, int):
            self._election_started.setdefault(term, event["ts"])
        elif event["type"] == "leader_elected" and isinstance(term, int):
            started = self._election_started.pop(term, None)
            if started is not None:
                duration = max(0, int(event["ts"] - started))
                self.last_election_ms = duration
                self._election_durations.append(duration)

    def _handle_network_event(self, event: dict[str, Any]) -> None:
        data = event.get("data") or {}
        if isinstance(data.get("latency_ms"), (int, float)) and data.get("ok"):
            self._network_latencies.append(float(data["latency_ms"]))
        self.record_event(
            event.get("type", "network.event"),
            event.get("message", ""),
            severity=event.get("severity", "info"),
            source="network",
            node_id=event.get("node_id"),
            data=data,
            ts=event.get("ts"),
        )

    def _add_node_event(self, event: dict[str, Any]) -> None:
        self._event_seq += 1
        enriched = {
            "seq": self._event_seq,
            "node_seq": event.get("seq"),
            "ts": event.get("ts") or int(time.time() * 1000),
            "source": "node",
            "node_id": event.get("node_id"),
            "type": event.get("type", "node.event"),
            "severity": event.get("severity", "info"),
            "message": event.get("message", ""),
            "data": event.get("data") or {},
        }
        self.events.append(enriched)
        if self._event_log:
            try:
                self._event_log.write(json.dumps(enriched) + "\n")
                self._event_log.flush()
            except (OSError, ValueError):
                pass
        self._broadcast({"type": "event", "event": enriched})
        self._track_election_event(enriched)

    def _broadcast(self, message: dict[str, Any]) -> None:
        if not self._subscribers:
            return
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:
                try:
                    queue.get_nowait()
                    queue.put_nowait(message)
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    pass

    def events_payload(
        self,
        *,
        node_id: str | None = None,
        event_type: str | None = None,
        severity: str | None = None,
        source: str | None = None,
        search: str | None = None,
        since: int | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        result = []
        for event in reversed(self.events):
            if node_id and event.get("node_id") != node_id:
                continue
            if event_type and not event.get("type", "").startswith(event_type):
                continue
            if severity and event.get("severity") != severity:
                continue
            if source and event.get("source") != source:
                continue
            if since and event.get("ts", 0) < since:
                continue
            if search:
                haystack = f"{event.get('message','')} {event.get('type','')}".lower()
                if search.lower() not in haystack:
                    continue
            result.append(event)
            if len(result) >= limit:
                break
        return list(reversed(result))

    # ----------------------------------------------------------------- lifecycle
    async def start(self, wait_for_leader: float = 6.0) -> None:
        if self._running:
            return
        os.makedirs(self.settings.data_dir, exist_ok=True)
        self._event_log = open(self._event_log_path, "a", encoding="utf-8")
        self.network.reset()
        self._running = True
        self._started_at = time.time()
        self.record_event(
            "cluster.starting",
            f"starting {len(self.specs)}-node cluster",
            data={"nodes": self.node_ids},
        )
        self.proxies = {
            spec.node_id: MeshProxy(
                spec.node_id,
                spec.direct_url,
                self.network,
                host=spec.host,
                port=spec.proxy_port,
            )
            for spec in self.specs
        }
        await asyncio.gather(*(proxy.start() for proxy in self.proxies.values()))
        spec_by_id = {spec.node_id: spec for spec in self.specs}
        self.proxy_urls = {
            node_id: f"http://{spec_by_id[node_id].host}:{proxy.port}"
            for node_id, proxy in self.proxies.items()
        }
        self.supervisor = ClusterSupervisor(
            self.specs,
            self.settings.raft,
            self.proxy_urls,
            seed=self.seed,
            env_overrides=self.env_overrides,
        )
        await self.supervisor.start_all()
        self._tasks = [
            asyncio.create_task(self._status_loop(), name="controller-status"),
            asyncio.create_task(self._metrics_loop(), name="controller-metrics"),
            asyncio.create_task(self._broadcast_loop(), name="controller-broadcast"),
        ]
        self.record_event(
            "cluster.started",
            f"all {len(self.specs)} node processes are ready",
            data={"proxy_urls": self.proxy_urls},
        )
        if wait_for_leader:
            await self.wait_for_leader(wait_for_leader)
        self._broadcast_snapshot()

    async def stop(self) -> None:
        if not self._running and not self.supervisor:
            return
        self.record_event("cluster.stopping", "stopping cluster")
        await self.stop_random_failures(restart=False)
        self._running = False
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
        if self.supervisor:
            await self.supervisor.stop_all()
            await self.supervisor.close()
            self.supervisor = None
        await asyncio.gather(
            *(proxy.stop() for proxy in self.proxies.values()), return_exceptions=True
        )
        self.proxies = {}
        self.online = {node_id: False for node_id in self.node_ids}
        for status in self.statuses.values():
            if status:
                status["role"] = "offline"
        self._broadcast({"type": "state", "data": self.cluster_snapshot()})
        self.record_event("cluster.stopped", "cluster stopped")
        if self._event_log:
            self._event_log.close()
            self._event_log = None
        self._broadcast_snapshot()

    async def shutdown(self) -> None:
        await self.stop()
        await self._client.aclose()

    async def restart_node(self, node_id: str) -> None:
        if not self.supervisor:
            raise RuntimeError("cluster is not running")
        self._validate_node(node_id)
        self.record_event(
            "node.restart_requested", f"restarting {node_id}", node_id=node_id, severity="warning"
        )
        await self.supervisor.restart_node(node_id)
        self._node_event_seq[node_id] = 0
        self.online[node_id] = False
        self.statuses[node_id] = None
        await self.refresh_node(node_id)
        self.record_event(
            "node.restarted",
            f"{node_id} restarted with persisted state",
            node_id=node_id,
            data={"pid": self.supervisor.describe()},
        )
        self._broadcast_snapshot()

    async def crash_node(self, node_id: str, source: str = "manual") -> None:
        if not self.supervisor:
            raise RuntimeError("cluster is not running")
        self._validate_node(node_id)
        self._require_running(node_id)
        role = (self.statuses.get(node_id) or {}).get("role", "unknown")
        exit_code = await self.supervisor.crash_node(node_id)
        self.online[node_id] = False
        status = self.statuses.get(node_id)
        if status:
            status["role"] = "offline"
            status["running"] = False
        self.record_event(
            "node.crashed",
            f"{node_id} killed (SIGKILL, was {role})",
            severity="critical",
            node_id=node_id,
            data={"exit_code": exit_code, "was_role": role, "initiator": source},
        )
        self._broadcast_snapshot()

    async def trigger_leader_failure(self) -> str | None:
        leader = self.current_leader()
        if not leader:
            await self.refresh_statuses()
            leader = self.current_leader()
        if not leader:
            raise RuntimeError("no leader to fail")
        await self.crash_node(leader, source="leader-failure")
        return leader

    def _validate_node(self, node_id: str) -> None:
        if node_id not in self.node_ids:
            raise KeyError(node_id)

    def _require_running(self, node_id: str) -> None:
        if not self.supervisor or not self.supervisor.is_running(node_id):
            raise RuntimeError(f"{node_id} is not running")

    # ------------------------------------------------------------------- polling
    async def _status_loop(self) -> None:
        while self._running:
            await self.refresh_statuses()
            await asyncio.sleep(0.35)

    async def refresh_statuses(self) -> None:
        if not self.supervisor:
            return
        await asyncio.gather(*(self.refresh_node(node_id) for node_id in self.node_ids))

    async def refresh_node(self, node_id: str) -> None:
        if not self.supervisor or not self.supervisor.is_running(node_id):
            self.online[node_id] = False
            return
        spec = next(spec for spec in self.specs if spec.node_id == node_id)
        try:
            response = await self._client.get(f"{spec.direct_url}/raft/status", timeout=1.0)
            response.raise_for_status()
            self.statuses[node_id] = response.json()
            self.online[node_id] = True
            self.last_seen[node_id] = time.time()
        except (httpx.HTTPError, ValueError):
            self.online[node_id] = False
            return
        try:
            after = self._node_event_seq[node_id]
            response = await self._client.get(
                f"{spec.direct_url}/raft/events",
                params={"after": after},
                timeout=1.0,
            )
            response.raise_for_status()
            payload = response.json()
            if payload.get("last_seq", 0) < after:
                self._node_event_seq[node_id] = 0
                after = 0
            for event in payload.get("events", []):
                if event.get("seq", 0) > after:
                    self._add_node_event(event)
                    after = event["seq"]
            self._node_event_seq[node_id] = after
        except (httpx.HTTPError, ValueError):
            pass

    async def wait_for_leader(
        self, timeout: float = 10.0, exclude: set[str] | None = None
    ) -> str | None:
        exclude = exclude or set()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            await self.refresh_statuses()
            leaders = [
                status
                for node_id, status in self.statuses.items()
                if status
                and self.online.get(node_id)
                and status.get("role") == "leader"
                and node_id not in exclude
            ]
            if leaders:
                return sorted(leaders, key=lambda s: s.get("term", 0), reverse=True)[0][
                    "node_id"
                ]
            await asyncio.sleep(0.1)
        return None

    async def _broadcast_loop(self) -> None:
        while self._running:
            await asyncio.sleep(0.5)
            self._broadcast_snapshot()

    def _broadcast_snapshot(self) -> None:
        if self._subscribers:
            self._broadcast({"type": "state", "data": self.cluster_snapshot()})

    # ---------------------------------------------------------------- aggregate view
    def cluster_snapshot(self) -> dict[str, Any]:
        nodes = []
        leader_status = None
        leaders: list[dict[str, Any]] = []
        supervisor_info = (
            {item["node_id"]: item for item in self.supervisor.describe()}
            if self.supervisor
            else {}
        )
        for spec in self.specs:
            status = self.statuses.get(spec.node_id)
            online = bool(self.online.get(spec.node_id))
            process_running = bool(
                self.supervisor and self.supervisor.is_running(spec.node_id)
            )
            entry: dict[str, Any] = {
                "node_id": spec.node_id,
                "online": online,
                "process_running": process_running,
                "http_port": spec.http_port,
                "proxy_port": spec.proxy_port,
                "last_seen_ms": (
                    int((time.time() - self.last_seen[spec.node_id]) * 1000)
                    if self.last_seen[spec.node_id]
                    else None
                ),
            }
            if status:
                entry.update(status)
                if not online:
                    entry["role"] = "offline"
            else:
                entry.update(
                    {
                        "role": "offline",
                        "term": 0,
                        "leader_id": None,
                        "last_log_index": 0,
                        "commit_index": 0,
                        "applied_index": 0,
                        "snapshot_index": 0,
                        "log_length": 0,
                        "counters": {},
                        "recent_commit_latency_ms": [],
                    }
                )
            entry["online"] = online
            entry["status"] = (
                "online" if online else ("crashed" if not process_running else "unreachable")
            )
            nodes.append(entry)
            if status and online and status.get("role") == "leader":
                leaders.append(status)
            info = supervisor_info.get(spec.node_id, {})
            entry["pid"] = info.get("pid")
            entry["crash_count"] = info.get("crash_count", 0)
            entry["restarts"] = info.get("restarts", 0)
            entry["inflight_operations"] = sum(
                1 for op in self._inflight.values() if op.get("leader") == spec.node_id
            )

        if leaders:
            leader_status = sorted(leaders, key=lambda s: s.get("term", 0), reverse=True)[0]
        active = sum(1 for node_id in self.node_ids if self.online[node_id])
        total = len(self.node_ids)
        quorum = total // 2 + 1
        max_term = max((node.get("term", 0) for node in nodes), default=0)
        same_term_leaders = [l for l in leaders if l.get("term") == max_term]
        split_brain = len(same_term_leaders) > 1
        if not self._running:
            health = "stopped"
        elif active < quorum or not leader_status or split_brain:
            health = "critical"
        elif active < total:
            health = "degraded"
        else:
            health = "healthy"

        latest_metrics = self.metrics_history[-1] if self.metrics_history else {}
        return {
            "status": "running" if self._running else "stopped",
            "health": health,
            "leader_id": leader_status["node_id"] if leader_status else None,
            "term": leader_status["term"] if leader_status else max_term,
            "active_nodes": active,
            "failed_nodes": total - active,
            "total_nodes": total,
            "quorum_size": quorum,
            "split_brain": split_brain,
            "commit_index": leader_status["commit_index"] if leader_status else 0,
            "applied_index": leader_status["applied_index"] if leader_status else 0,
            "total_log_entries": sum(node.get("last_log_index", 0) for node in nodes),
            "throughput": latest_metrics.get("throughput", 0.0),
            "avg_latency_ms": latest_metrics.get("write_latency_avg_ms", 0.0),
            "p95_latency_ms": latest_metrics.get("write_latency_p95_ms", 0.0),
            "election_ms": self.last_election_ms,
            "uptime_s": round(time.time() - self._started_at, 1) if self._started_at else 0,
            "nodes": nodes,
            "simulation": {
                "network": self.network.describe(),
                "random_failures": self.random_state,
                "inflight_operations": len(self._inflight),
            },
            "timestamp": int(time.time() * 1000),
        }

    # ------------------------------------------------------------------ metrics
    async def _metrics_loop(self) -> None:
        while self._running:
            await asyncio.sleep(1.0)
            self._sample_metrics()

    def _sample_metrics(self) -> None:
        now = time.time()
        leader = self.current_leader_status()
        leader_id = leader["node_id"] if leader else None
        commit_index = leader.get("commit_index", 0) if leader else 0
        prev_ts, prev_commit, prev_leader = self._last_commit_sample
        throughput = 0.0
        if prev_ts and leader_id == prev_leader and now > prev_ts:
            throughput = max(0.0, (commit_index - prev_commit) / (now - prev_ts))
        self._last_commit_sample = (now, commit_index, leader_id)

        latencies = self._latencies
        read_latencies = self._read_latencies
        self._latencies = []
        self._read_latencies = []
        network_latencies = list(self._network_latencies)
        replication_latency = 0.0
        if leader:
            recent = leader.get("recent_commit_latency_ms") or []
            if recent:
                replication_latency = round(statistics.mean(recent[-10:]), 2)
        active = sum(1 for node_id in self.node_ids if self.online[node_id])
        availability = round(100.0 * active / len(self.node_ids), 1)
        sample = {
            "ts": int(now * 1000),
            "throughput": round(throughput, 2),
            "write_latency_avg_ms": round(statistics.mean(latencies), 2) if latencies else 0.0,
            "write_latency_p50_ms": percentile(latencies, 50),
            "write_latency_p95_ms": percentile(latencies, 95),
            "write_latency_p99_ms": percentile(latencies, 99),
            "read_latency_avg_ms": (
                round(statistics.mean(read_latencies), 2) if read_latencies else 0.0
            ),
            "read_latency_p95_ms": percentile(read_latencies, 95),
            "replication_latency_ms": replication_latency,
            "election_ms": self.last_election_ms or 0,
            "node_availability_pct": availability,
            "network_latency_ms": (
                round(statistics.mean(network_latencies), 2) if network_latencies else 0.0
            ),
            "commit_index": commit_index,
            "leader_id": leader_id,
            "term": leader.get("term", 0) if leader else 0,
            "successful_ops": self._interval_success,
            "failed_ops": self._interval_failure,
            "active_nodes": active,
        }
        self.metrics_history.append(sample)
        self._interval_success = 0
        self._interval_failure = 0

    def metrics_payload(self) -> dict[str, Any]:
        return {
            "current": self.metrics_history[-1] if self.metrics_history else {},
            "history": list(self.metrics_history),
            "election_durations_ms": list(self._election_durations),
            "totals": {
                "successful_ops": self._total_success,
                "failed_ops": self._total_failure,
            },
        }

    def record_client_latency(self, latency_ms: float, kind: str = "write") -> None:
        if kind == "read":
            self._read_latencies.append(latency_ms)
        else:
            self._latencies.append(latency_ms)

    # ------------------------------------------------------------------ KV gateway
    async def kv_command(
        self,
        op: str,
        key: str,
        value: str | None = None,
        *,
        retries: int = 2,
    ) -> dict[str, Any]:
        if op not in ("set", "delete"):
            raise ValueError("op must be set or delete")
        attempts = retries + 1
        last_error: Exception | None = None
        for attempt in range(attempts):
            leader = await self._ensure_leader()
            if not leader:
                last_error = RuntimeError("no leader available")
                await asyncio.sleep(0.15)
                continue
            spec = next(spec for spec in self.specs if spec.node_id == leader)
            started = time.monotonic()
            op_record = {
                "op": op,
                "key": key,
                "leader": leader,
                "started": time.time(),
                "status": "pending",
                "attempt": attempt + 1,
            }
            op_id = f"{int(time.time()*1000)}-{key}-{attempt}"
            self._inflight[op_id] = op_record
            try:
                response = await self._client.post(
                    f"{spec.direct_url}/kv",
                    json={"op": op, "key": key, "value": value},
                    timeout=8.0,
                )
                latency_ms = (time.monotonic() - started) * 1000
                self.record_client_latency(latency_ms, "write")
                if response.status_code == 200:
                    payload = response.json()
                    op_record["status"] = "committed"
                    self._interval_success += 1
                    self._total_success += 1
                    self.record_event(
                        "client.operation_committed",
                        f"{op.upper()} {key} committed at index {payload.get('index')}",
                        source="client",
                        node_id=leader,
                        data={"op": op, "key": key, **payload, "latency_ms": round(latency_ms, 2)},
                    )
                    return payload
                if response.status_code == 409:
                    self._inflight.pop(op_id, None)
                    await asyncio.sleep(0.1)
                    continue
                if response.status_code in (503, 504):
                    last_error = RuntimeError(
                        f"leader {leader} could not commit: {response.text[:200]}"
                    )
                    await asyncio.sleep(0.15)
                    continue
                raise RuntimeError(
                    f"unexpected node response {response.status_code}: {response.text[:200]}"
                )
            except (httpx.HTTPError, RuntimeError) as exc:
                last_error = exc
                await asyncio.sleep(0.15)
            finally:
                if op_id in self._inflight and self._inflight[op_id].get("status") == "pending":
                    self._inflight[op_id]["status"] = "failed"
                    self._inflight.pop(op_id, None)
        self._interval_failure += 1
        self._total_failure += 1
        self.record_event(
            "client.operation_failed",
            f"{op.upper()} {key} failed: {last_error}",
            severity="warning",
            source="client",
            data={"op": op, "key": key, "error": str(last_error)},
        )
        raise RuntimeError(str(last_error) if last_error else "operation failed")

    async def _ensure_leader(self) -> str | None:
        leader = self.current_leader()
        if leader and self.online.get(leader):
            return leader
        await self.refresh_statuses()
        return self.current_leader()

    async def kv_get(self, key: str) -> dict[str, Any]:
        last_error: Exception | None = None
        for _ in range(3):
            leader = await self._ensure_leader()
            if not leader:
                last_error = RuntimeError("no leader available")
                await asyncio.sleep(0.1)
                continue
            spec = next(spec for spec in self.specs if spec.node_id == leader)
            started = time.monotonic()
            try:
                response = await self._client.get(f"{spec.direct_url}/kv/{key}", timeout=5.0)
                latency_ms = (time.monotonic() - started) * 1000
                self.record_client_latency(latency_ms, "read")
                if response.status_code == 200:
                    return response.json()
                if response.status_code in (409, 503):
                    last_error = RuntimeError(response.text[:200])
                    await asyncio.sleep(0.1)
                    continue
                raise RuntimeError(f"node returned {response.status_code}")
            except httpx.HTTPError as exc:
                last_error = exc
                await asyncio.sleep(0.1)
        raise RuntimeError(str(last_error) if last_error else "read failed")

    async def kv_list(self) -> dict[str, Any]:
        leader = await self._ensure_leader()
        candidates = [leader] if leader else []
        candidates += [
            n for n in sorted(
                self.node_ids,
                key=lambda nid: (self.statuses.get(nid) or {}).get("applied_index", 0),
                reverse=True,
            )
            if n not in candidates and self.online.get(n)
        ]
        for node_id in candidates:
            spec = next(spec for spec in self.specs if spec.node_id == node_id)
            try:
                response = await self._client.get(f"{spec.direct_url}/kv", timeout=1.5)
                if response.status_code == 200:
                    payload = response.json()
                    payload.update(
                        {
                            "source": node_id,
                            "stale": node_id != leader,
                            "commit_index": (self.statuses.get(node_id) or {}).get(
                                "commit_index", 0
                            ),
                        }
                    )
                    return payload
            except httpx.HTTPError:
                continue
        raise RuntimeError("no reachable node to read key-value state")

    async def log_view(self, limit: int = 200) -> dict[str, Any]:
        leader = await self._ensure_leader()
        candidates = [leader] if leader else []
        candidates += [n for n in self.node_ids if n not in candidates and self.online.get(n)]
        for node_id in candidates:
            if node_id is None:
                continue
            spec = next(spec for spec in self.specs if spec.node_id == node_id)
            try:
                response = await self._client.get(
                    f"{spec.direct_url}/raft/log", params={"start": 1}, timeout=2.0
                )
                if response.status_code == 200:
                    payload = response.json()
                    payload["entries"] = payload["entries"][-limit:]
                    payload["source"] = node_id
                    payload["is_leader"] = node_id == leader
                    return payload
            except httpx.HTTPError:
                continue
        raise RuntimeError("no reachable node for log inspection")

    # ---------------------------------------------------------------- fault controls
    def set_partition(self, groups: list[list[str]]) -> None:
        self.network.set_partition(groups)
        self.record_event(
            "simulation.partition_applied",
            "partition rule applied",
            severity="warning",
            data={"groups": groups},
        )
        self._broadcast_snapshot()

    def heal(self) -> None:
        self.network.heal()
        self.record_event("simulation.heal", "network partitions healed")
        self._broadcast_snapshot()

    def set_latency(
        self, latency_ms: float, source: str | None = None, target: str | None = None
    ) -> None:
        if latency_ms < 0 or latency_ms > 60_000:
            raise ValueError("latency must be between 0 and 60000 ms")
        for node_id in (source, target):
            if node_id and node_id not in self.node_ids:
                raise ValueError(f"unknown node {node_id}")
        self.network.set_latency(latency_ms, source, target)
        self._broadcast_snapshot()

    def set_loss(
        self, loss: float, source: str | None = None, target: str | None = None
    ) -> None:
        if not 0.0 <= loss <= 1.0:
            raise ValueError("loss must be between 0 and 1")
        for node_id in (source, target):
            if node_id and node_id not in self.node_ids:
                raise ValueError(f"unknown node {node_id}")
        self.network.set_loss(loss, source, target)
        self._broadcast_snapshot()

    def reset_faults(self) -> None:
        self.network.reset()
        self.record_event("simulation.reset", "all network faults cleared")
        self._broadcast_snapshot()

    async def start_random_failures(
        self,
        probability: float,
        interval_s: float,
        recovery_delay_s: float,
        max_failures: int,
        seed: int,
        duration_s: float | None = None,
    ) -> None:
        if not 0 < probability <= 1:
            raise ValueError("probability must be in (0, 1]")
        if not 0.2 <= interval_s <= 60:
            raise ValueError("interval must be between 0.2 and 60 seconds")
        if not 0 <= recovery_delay_s <= 120:
            raise ValueError("recovery_delay must be between 0 and 120 seconds")
        if max_failures < 1:
            raise ValueError("max_failures must be at least 1")
        if duration_s is not None and duration_s <= 0:
            raise ValueError("duration must be positive")
        await self.stop_random_failures(restart=True)
        self.random_state = {
            "running": True,
            "config": {
                "probability": probability,
                "interval_s": interval_s,
                "recovery_delay_s": recovery_delay_s,
                "max_failures": max_failures,
                "seed": seed,
                "duration_s": duration_s,
            },
            "failures": 0,
            "recoveries": 0,
        }
        self.record_event(
            "simulation.random_failures_started",
            f"random failure experiment started (p={probability}, every {interval_s}s)",
            severity="warning",
            data=self.random_state["config"],
        )
        self._random_task = asyncio.create_task(
            self._random_failure_loop(probability, interval_s, recovery_delay_s, max_failures, seed, duration_s),
            name="random-failures",
        )
        self._broadcast_snapshot()

    async def _random_failure_loop(
        self,
        probability: float,
        interval_s: float,
        recovery_delay_s: float,
        max_failures: int,
        seed: int,
        duration_s: float | None,
    ) -> None:
        rng = random.Random(seed)
        started = time.monotonic()
        try:
            while self._running:
                await asyncio.sleep(interval_s)
                if duration_s and time.monotonic() - started >= duration_s:
                    break
                if self.random_state["failures"] >= max_failures:
                    if not self._random_recovery_tasks:
                        break
                    continue
                live = [n for n in self.node_ids if self.online.get(n)]
                for node_id in live:
                    if self.random_state["failures"] >= max_failures:
                        break
                    if rng.random() < probability:
                        try:
                            await self.crash_node(node_id, source="random-experiment")
                        except RuntimeError:
                            continue
                        self.random_state["failures"] += 1
                        self._random_crashed.add(node_id)
                        task = asyncio.create_task(
                            self._delayed_restart(node_id, recovery_delay_s),
                            name=f"recover-{node_id}",
                        )
                        self._random_recovery_tasks.add(task)
                        task.add_done_callback(self._random_recovery_tasks.discard)
        except asyncio.CancelledError:
            raise
        finally:
            self.random_state["running"] = False
            self._broadcast_snapshot()

    async def _delayed_restart(self, node_id: str, delay: float) -> None:
        await asyncio.sleep(delay)
        if not self._running or not self.supervisor:
            return
        try:
            started = time.monotonic()
            await self.supervisor.restart_node(node_id)
            self._node_event_seq[node_id] = 0
            self.online[node_id] = False
            self.statuses[node_id] = None
            await self.refresh_node(node_id)
            self.random_state["recoveries"] += 1
            self._random_crashed.discard(node_id)
            self.record_event(
                "simulation.random_recovery",
                f"{node_id} automatically recovered after {delay}s",
                severity="info",
                node_id=node_id,
                data={"recovery_s": round(time.monotonic() - started, 2)},
            )
        except (NodeProcessError, RuntimeError) as exc:
            self.record_event(
                "simulation.recovery_failed",
                f"automatic recovery of {node_id} failed: {exc}",
                severity="error",
                node_id=node_id,
            )
        self._broadcast_snapshot()

    async def stop_random_failures(self, restart: bool = True) -> None:
        if self._random_task:
            self._random_task.cancel()
            await asyncio.gather(self._random_task, return_exceptions=True)
            self._random_task = None
        for task in list(self._random_recovery_tasks):
            task.cancel()
        await asyncio.gather(*self._random_recovery_tasks, return_exceptions=True)
        self._random_recovery_tasks.clear()
        was_running = self.random_state.get("running")
        self.random_state["running"] = False
        if restart and self.supervisor:
            for node_id in sorted(self._random_crashed):
                try:
                    await self.supervisor.restart_node(node_id)
                    self._node_event_seq[node_id] = 0
                    self.online[node_id] = False
                    self.statuses[node_id] = None
                    await self.refresh_node(node_id)
                    self.record_event(
                        "node.restarted",
                        f"{node_id} restored when experiment stopped",
                        node_id=node_id,
                    )
                except (NodeProcessError, RuntimeError) as exc:
                    logger.warning("failed to restore %s: %s", node_id, exc)
            self._random_crashed.clear()
        if was_running:
            self.record_event(
                "simulation.random_failures_stopped", "random failure experiment stopped"
            )
        self._broadcast_snapshot()

    # ------------------------------------------------------------- experiment store
    def _load_experiment_index(self) -> None:
        try:
            for name in sorted(os.listdir(self._experiments_dir), reverse=True):
                if not name.endswith(".json"):
                    continue
                path = os.path.join(self._experiments_dir, name)
                try:
                    with open(path, encoding="utf-8") as handle:
                        report = json.load(handle)
                    if report.get("id"):
                        self._experiments[report["id"]] = report
                except (OSError, ValueError):
                    logger.warning("could not load experiment report %s", path)
        except OSError:
            pass

    async def save_experiment(self, report: dict[str, Any]) -> None:
        os.makedirs(self._experiments_dir, exist_ok=True)
        path = os.path.join(self._experiments_dir, f"{report['id']}.json")
        tmp = path + ".tmp"
        await asyncio.to_thread(self._write_json, tmp, path, report)
        self._experiments[report["id"]] = report

    @staticmethod
    def _write_json(tmp: str, path: str, payload: dict[str, Any]) -> None:
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)

    def list_experiments(self) -> list[dict[str, Any]]:
        reports = sorted(
            self._experiments.values(), key=lambda r: r.get("created_at", 0), reverse=True
        )
        summaries = []
        for report in reports:
            summaries.append(
                {
                    "id": report.get("id"),
                    "created_at": report.get("created_at"),
                    "scenario": report.get("config", {}).get("scenario"),
                    "kind": report.get("kind", "experiment"),
                    "duration_s": report.get("config", {}).get("duration_s"),
                    "outcome": report.get("outcome"),
                    "consistency_passed": report.get("consistency", {}).get("passed"),
                    "summary": report.get("summary", {}),
                }
            )
        return summaries

    def get_experiment(self, experiment_id: str) -> dict[str, Any] | None:
        return self._experiments.get(experiment_id)
