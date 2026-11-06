"""Shared configuration for the Raft cluster and controller."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    return float(value) if value not in (None, "") else default


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    return int(value) if value not in (None, "") else default


@dataclass(frozen=True)
class RaftSettings:
    election_timeout_min: float = 0.8
    election_timeout_max: float = 1.5
    heartbeat_interval: float = 0.2
    rpc_timeout: float = 1.5
    snapshot_threshold: int = 128
    client_timeout: float = 6.0
    max_append_entries: int = 64

    def __post_init__(self) -> None:
        if self.election_timeout_min <= 0:
            raise ValueError("election_timeout_min must be positive")
        if self.election_timeout_max < self.election_timeout_min:
            raise ValueError("election_timeout_max must be >= election_timeout_min")
        if self.heartbeat_interval <= 0:
            raise ValueError("heartbeat_interval must be positive")
        if self.heartbeat_interval >= self.election_timeout_min:
            raise ValueError("heartbeat_interval must be shorter than election_timeout_min")
        if self.rpc_timeout <= 0:
            raise ValueError("rpc_timeout must be positive")
        if self.client_timeout <= 0:
            raise ValueError("client_timeout must be positive")
        if self.snapshot_threshold < 0:
            raise ValueError("snapshot_threshold must be >= 0 (0 disables snapshots)")
        if self.max_append_entries < 1:
            raise ValueError("max_append_entries must be >= 1")

    @classmethod
    def from_env(cls) -> "RaftSettings":
        return cls(
            election_timeout_min=_env_float("ELECTION_TIMEOUT_MIN", 0.8),
            election_timeout_max=_env_float("ELECTION_TIMEOUT_MAX", 1.5),
            heartbeat_interval=_env_float("HEARTBEAT_INTERVAL", 0.2),
            rpc_timeout=_env_float("RPC_TIMEOUT", 1.5),
            snapshot_threshold=_env_int("SNAPSHOT_THRESHOLD", 128),
            client_timeout=_env_float("CLIENT_TIMEOUT", 6.0),
        )


@dataclass(frozen=True)
class NodeSpec:
    node_id: str
    http_port: int
    proxy_port: int
    data_dir: str
    host: str = "127.0.0.1"

    @property
    def direct_url(self) -> str:
        return f"http://{self.host}:{self.http_port}"

    @property
    def proxy_url(self) -> str:
        return f"http://{self.host}:{self.proxy_port}"


@dataclass
class ClusterSettings:
    cluster_size: int = 3
    host: str = "127.0.0.1"
    node_base_port: int = 7001
    proxy_base_port: int = 7101
    data_dir: str = "data"
    auto_start: bool = True
    raft: RaftSettings = field(default_factory=RaftSettings.from_env)

    @classmethod
    def from_env(cls) -> "ClusterSettings":
        return cls(
            cluster_size=_env_int("CLUSTER_SIZE", 3),
            host=os.getenv("CLUSTER_HOST", "127.0.0.1"),
            node_base_port=_env_int("NODE_BASE_PORT", 7001),
            proxy_base_port=_env_int("PROXY_BASE_PORT", 7101),
            data_dir=os.getenv("DATA_DIR", "data"),
            auto_start=os.getenv("AUTO_START", "true").lower() != "false",
            raft=RaftSettings.from_env(),
        )

    def build_nodes(self) -> list[NodeSpec]:
        if self.cluster_size < 3 or self.cluster_size % 2 == 0:
            raise ValueError("cluster_size must be an odd number >= 3")
        nodes: list[NodeSpec] = []
        for i in range(self.cluster_size):
            node_id = f"n{i + 1}"
            nodes.append(
                NodeSpec(
                    node_id=node_id,
                    http_port=self.node_base_port + i,
                    proxy_port=self.proxy_base_port + i,
                    data_dir=os.path.join(self.data_dir, f"node-{node_id}"),
                    host=self.host,
                )
            )
        return nodes
