"""Shared fixtures: real node subprocesses with isolated ports and data directories."""

from __future__ import annotations

import socket

import pytest_asyncio

from backend.config import ClusterSettings, NodeSpec, RaftSettings
from backend.simulator.controller import ClusterController


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def make_settings(tmp_path, size: int = 3, **raft_overrides) -> ClusterSettings:
    defaults = dict(
        election_timeout_min=0.4,
        election_timeout_max=0.8,
        heartbeat_interval=0.1,
        rpc_timeout=0.8,
        snapshot_threshold=64,
        client_timeout=5.0,
    )
    defaults.update(raft_overrides)
    return ClusterSettings(
        cluster_size=size,
        data_dir=str(tmp_path / "data"),
        auto_start=False,
        raft=RaftSettings(**defaults),
    )


def make_specs(tmp_path, size: int = 3) -> list[NodeSpec]:
    specs = []
    for index in range(size):
        specs.append(
            NodeSpec(
                node_id=f"n{index + 1}",
                http_port=free_port(),
                proxy_port=free_port(),
                data_dir=str(tmp_path / f"node-n{index + 1}"),
            )
        )
    return specs


@pytest_asyncio.fixture
async def cluster(tmp_path):
    settings = make_settings(tmp_path)
    specs = make_specs(tmp_path)
    controller = ClusterController(settings, specs, seed=11)
    await controller.start(wait_for_leader=0)
    try:
        yield controller
    finally:
        await controller.shutdown()
