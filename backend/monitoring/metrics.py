"""Prometheus exposition derived from live controller state."""

from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Gauge, generate_latest

CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"


class RaftMetrics:
    def __init__(self) -> None:
        self.registry = CollectorRegistry()
        self.node_up = Gauge(
            "raft_node_up", "Node process reachable", ["node"], registry=self.registry
        )
        self.node_term = Gauge(
            "raft_node_term", "Current Raft term", ["node"], registry=self.registry
        )
        self.node_commit = Gauge(
            "raft_node_commit_index", "Commit index", ["node"], registry=self.registry
        )
        self.node_applied = Gauge(
            "raft_node_applied_index", "Applied index", ["node"], registry=self.registry
        )
        self.node_log = Gauge(
            "raft_node_last_log_index", "Last log index", ["node"], registry=self.registry
        )
        self.node_snapshot = Gauge(
            "raft_node_snapshot_index", "Snapshot base index", ["node"], registry=self.registry
        )
        self.node_is_leader = Gauge(
            "raft_node_is_leader", "1 when this node is leader", ["node"], registry=self.registry
        )
        self.node_role = Gauge(
            "raft_node_role",
            "1 when the node currently has this role",
            ["node", "role"],
            registry=self.registry,
        )
        self.node_kv = Gauge(
            "raft_node_kv_keys", "Materialized keys", ["node"], registry=self.registry
        )
        self.node_rpc_sent = Gauge(
            "raft_node_rpc_sent", "RPCs sent by this node since it started", ["node"], registry=self.registry
        )
        self.node_rpc_failed = Gauge(
            "raft_node_rpc_failed", "Failed RPCs since this node started", ["node"], registry=self.registry
        )
        self.node_elections_started = Gauge(
            "raft_node_elections_started", "Elections started since node start", ["node"], registry=self.registry
        )
        self.node_elections_won = Gauge(
            "raft_node_elections_won", "Elections won since node start", ["node"], registry=self.registry
        )
        self.node_snapshots_created = Gauge(
            "raft_node_snapshots_created", "Snapshots created since node start", ["node"], registry=self.registry
        )
        self.node_snapshots_installed = Gauge(
            "raft_node_snapshots_installed", "Snapshots installed since node start", ["node"], registry=self.registry
        )
        self.node_client_write_failures = Gauge(
            "raft_node_client_write_failures", "Client writes failed since node start", ["node"], registry=self.registry
        )
        self.cluster_leader = Gauge(
            "raft_cluster_has_leader", "1 when a leader is known", registry=self.registry
        )
        self.cluster_term = Gauge("raft_cluster_term", "Highest observed term", registry=self.registry)
        self.cluster_commit = Gauge(
            "raft_cluster_commit_index", "Leader commit index", registry=self.registry
        )
        self.cluster_active = Gauge(
            "raft_cluster_active_nodes", "Reachable nodes", registry=self.registry
        )
        self.cluster_quorum = Gauge(
            "raft_cluster_quorum_size", "Majority size", registry=self.registry
        )
        self.cluster_throughput = Gauge(
            "raft_cluster_committed_entries_per_second",
            "Leader commit-index growth per second; includes replicated no-op entries",
            registry=self.registry,
        )
        self.cluster_latency = Gauge(
            "raft_cluster_write_latency_p95_ms", "P95 client write latency", registry=self.registry
        )
        self.cluster_replication = Gauge(
            "raft_cluster_replication_latency_ms", "Mean leader commit latency", registry=self.registry
        )
        self.cluster_availability = Gauge(
            "raft_cluster_availability_ratio", "Active / total nodes", registry=self.registry
        )
        self.cluster_election = Gauge(
            "raft_cluster_last_election_ms", "Last observed election duration", registry=self.registry
        )
        self.cluster_network_latency = Gauge(
            "raft_cluster_network_latency_ms", "Mean inter-node RPC latency", registry=self.registry
        )
        self.client_success = Counter(
            "raft_client_successful_writes_total",
            "Client writes acknowledged since controller start",
            registry=self.registry,
        )
        self.client_failure = Counter(
            "raft_client_failed_writes_total",
            "Client writes that exhausted retries since controller start",
            registry=self.registry,
        )
        self._last_success = 0
        self._last_failure = 0
        self.network_forwarded = Gauge(
            "raft_network_forwarded_messages",
            "Messages forwarded by the network simulator since it started",
            registry=self.registry,
        )
        self.network_dropped = Gauge(
            "raft_network_dropped_messages",
            "Messages dropped by the network simulator since it started",
            registry=self.registry,
        )
        self.cluster_health = Gauge(
            "raft_cluster_health", "1 healthy, 0.5 degraded, 0 critical", registry=self.registry
        )

    def update(self, controller) -> None:
        snapshot = controller.cluster_snapshot()
        metrics = controller.metrics_payload()
        current = metrics.get("current") or {}
        for node in snapshot["nodes"]:
            node_id = node["node_id"]
            role = node.get("role", "offline")
            self.node_up.labels(node=node_id).set(1 if node.get("online") else 0)
            self.node_term.labels(node=node_id).set(node.get("term", 0))
            self.node_commit.labels(node=node_id).set(node.get("commit_index", 0))
            self.node_applied.labels(node=node_id).set(node.get("applied_index", 0))
            self.node_log.labels(node=node_id).set(node.get("last_log_index", 0))
            self.node_snapshot.labels(node=node_id).set(node.get("snapshot_index", 0))
            self.node_is_leader.labels(node=node_id).set(1 if role == "leader" else 0)
            self.node_kv.labels(node=node_id).set(node.get("kv_count", 0))
            counters = node.get("counters") or {}
            self.node_rpc_sent.labels(node=node_id).set(counters.get("rpc_sent", 0))
            self.node_rpc_failed.labels(node=node_id).set(counters.get("rpc_failed", 0))
            self.node_elections_started.labels(node=node_id).set(
                counters.get("elections_started", 0)
            )
            self.node_elections_won.labels(node=node_id).set(counters.get("elections_won", 0))
            self.node_snapshots_created.labels(node=node_id).set(
                counters.get("snapshots_created", 0)
            )
            self.node_snapshots_installed.labels(node=node_id).set(
                counters.get("snapshots_installed", 0)
            )
            self.node_client_write_failures.labels(node=node_id).set(
                counters.get("client_write_failures", 0)
            )
            for possible in ("follower", "candidate", "leader", "offline"):
                self.node_role.labels(node=node_id, role=possible).set(
                    1 if role == possible else 0
                )
        self.cluster_leader.set(1 if snapshot["leader_id"] else 0)
        self.cluster_term.set(snapshot["term"])
        self.cluster_commit.set(snapshot["commit_index"])
        self.cluster_active.set(snapshot["active_nodes"])
        self.cluster_quorum.set(snapshot["quorum_size"])
        self.cluster_throughput.set(snapshot.get("throughput", 0.0))
        self.cluster_latency.set(snapshot.get("p95_latency_ms", 0.0))
        self.cluster_replication.set(current.get("replication_latency_ms", 0.0))
        self.cluster_availability.set(
            snapshot["active_nodes"] / max(1, snapshot["total_nodes"])
        )
        self.cluster_election.set(snapshot.get("election_ms") or 0)
        self.cluster_network_latency.set(current.get("network_latency_ms", 0.0))
        totals = metrics.get("totals", {})
        successful = int(totals.get("successful_ops", 0))
        failed = int(totals.get("failed_ops", 0))
        if successful >= self._last_success:
            self.client_success.inc(successful - self._last_success)
        self._last_success = successful
        if failed >= self._last_failure:
            self.client_failure.inc(failed - self._last_failure)
        self._last_failure = failed
        forwarded = sum(getattr(proxy, "forwarded", 0) for proxy in controller.proxies.values())
        dropped = sum(getattr(proxy, "dropped", 0) for proxy in controller.proxies.values())
        self.network_forwarded.set(forwarded)
        self.network_dropped.set(dropped)
        health_value = {"healthy": 1.0, "degraded": 0.5}.get(snapshot["health"], 0.0)
        self.cluster_health.set(health_value)

    def render(self, controller) -> tuple[bytes, str]:
        self.update(controller)
        return generate_latest(self.registry), CONTENT_TYPE
