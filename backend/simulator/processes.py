"""Supervision of independent Raft node OS processes."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from backend.config import NodeSpec, RaftSettings

logger = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parents[2]


class NodeProcessError(RuntimeError):
    pass


@dataclass
class NodeRuntime:
    spec: NodeSpec
    process: subprocess.Popen[bytes] | None = None
    log_handle: Any = None
    started_at: float | None = None
    crash_count: int = 0
    restarts: int = 0
    last_exit_code: int | None = None
    manually_stopped: bool = False


class ClusterSupervisor:
    def __init__(
        self,
        specs: list[NodeSpec],
        settings: RaftSettings,
        proxy_urls: dict[str, str],
        seed: int = 7,
        env_overrides: dict[str, str] | None = None,
    ) -> None:
        self.specs = {spec.node_id: spec for spec in specs}
        self.settings = settings
        self.proxy_urls = dict(proxy_urls)
        self.seed = seed
        self.env_overrides = dict(env_overrides or {})
        self.runtimes: dict[str, NodeRuntime] = {
            spec.node_id: NodeRuntime(spec=spec) for spec in specs
        }
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(2.0, connect=0.5))

    def peers_for(self, node_id: str) -> dict[str, str]:
        return {
            other: self.proxy_urls[other] for other in self.specs if other != node_id
        }

    def is_running(self, node_id: str) -> bool:
        runtime = self.runtimes[node_id]
        return runtime.process is not None and runtime.process.poll() is None

    def running_ids(self) -> list[str]:
        return [node_id for node_id in self.specs if self.is_running(node_id)]

    def log_path(self, node_id: str) -> Path:
        return Path(self.runtimes[node_id].spec.data_dir) / "node.log"

    def tail_log(self, node_id: str, lines: int = 60) -> str:
        path = self.log_path(node_id)
        if not path.exists():
            return ""
        try:
            content = path.read_text(errors="replace").splitlines()
            return "\n".join(content[-lines:])
        except OSError:
            return ""

    def _env(self) -> dict[str, str]:
        env = os.environ.copy()
        env.update(
            {
                "PYTHONUNBUFFERED": "1",
                "PYTHONPATH": str(PROJECT_ROOT),
                "ELECTION_TIMEOUT_MIN": str(self.settings.election_timeout_min),
                "ELECTION_TIMEOUT_MAX": str(self.settings.election_timeout_max),
                "HEARTBEAT_INTERVAL": str(self.settings.heartbeat_interval),
                "RPC_TIMEOUT": str(self.settings.rpc_timeout),
                "SNAPSHOT_THRESHOLD": str(self.settings.snapshot_threshold),
                "CLIENT_TIMEOUT": str(self.settings.client_timeout),
            }
        )
        env.update(self.env_overrides)
        return env

    @staticmethod
    def _seed_for(node_id: str) -> int:
        digits = "".join(ch for ch in node_id if ch.isdigit())
        return int(digits) if digits else 1

    async def start_node(self, node_id: str, wait: bool = True) -> None:
        runtime = self.runtimes[node_id]
        if self.is_running(node_id):
            return
        spec = runtime.spec
        os.makedirs(spec.data_dir, exist_ok=True)
        log_path = self.log_path(node_id)
        runtime.log_handle = open(log_path, "ab", buffering=0)
        peers = self.peers_for(node_id)
        command = [
            sys.executable,
            "-m",
            "backend.raft.node_main",
            "--id",
            node_id,
            "--port",
            str(spec.http_port),
            "--peers",
            json.dumps(peers),
            "--data-dir",
            spec.data_dir,
            "--host",
            spec.host,
            "--seed",
            str(self._seed_for(node_id)),
        ]
        logger.info("starting %s: %s", node_id, " ".join(command))
        runtime.process = subprocess.Popen(
            command,
            cwd=str(PROJECT_ROOT),
            env=self._env(),
            stdout=runtime.log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        runtime.started_at = time.time()
        runtime.manually_stopped = False
        if wait:
            await self.wait_ready(node_id)

    async def wait_ready(self, node_id: str, timeout: float = 20.0) -> None:
        runtime = self.runtimes[node_id]
        deadline = time.monotonic() + timeout
        url = f"{runtime.spec.direct_url}/health"
        while time.monotonic() < deadline:
            if runtime.process is None or runtime.process.poll() is not None:
                raise NodeProcessError(
                    f"{node_id} exited during startup\n{self.tail_log(node_id)}"
                )
            try:
                response = await self._client.get(url, timeout=0.5)
                if response.status_code == 200:
                    data = response.json()
                    if data.get("running"):
                        return
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.1)
        raise NodeProcessError(
            f"{node_id} did not become ready within {timeout}s\n{self.tail_log(node_id)}"
        )

    async def start_all(self) -> None:
        await asyncio.gather(*(self.start_node(node_id) for node_id in self.specs))

    async def crash_node(self, node_id: str) -> int | None:
        runtime = self.runtimes[node_id]
        if not self.is_running(node_id):
            return runtime.last_exit_code
        assert runtime.process is not None
        logger.warning("SIGKILL %s (pid %s)", node_id, runtime.process.pid)
        try:
            os.killpg(os.getpgid(runtime.process.pid), signal.SIGKILL)
        except ProcessLookupError:
            runtime.process.kill()
        exit_code = await asyncio.to_thread(runtime.process.wait)
        runtime.last_exit_code = exit_code
        runtime.crash_count += 1
        runtime.process = None
        if runtime.log_handle:
            runtime.log_handle.close()
            runtime.log_handle = None
        return exit_code

    async def stop_node(self, node_id: str) -> None:
        runtime = self.runtimes[node_id]
        if runtime.process is None:
            return
        runtime.manually_stopped = True
        process = runtime.process
        if process.poll() is None:
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(asyncio.to_thread(process.wait), timeout=8)
            except asyncio.TimeoutError:
                try:
                    os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await asyncio.to_thread(process.wait)
        runtime.last_exit_code = process.returncode
        runtime.process = None
        if runtime.log_handle:
            runtime.log_handle.close()
            runtime.log_handle = None

    async def restart_node(self, node_id: str) -> None:
        runtime = self.runtimes[node_id]
        if self.is_running(node_id):
            await self.stop_node(node_id)
        runtime.restarts += 1
        await self.start_node(node_id)

    async def stop_all(self) -> None:
        await asyncio.gather(*(self.stop_node(node_id) for node_id in self.specs))

    async def close(self) -> None:
        await self._client.aclose()

    def describe(self) -> list[dict[str, Any]]:
        descriptions = []
        for node_id, runtime in self.runtimes.items():
            process = runtime.process
            descriptions.append(
                {
                    "node_id": node_id,
                    "pid": process.pid if process else None,
                    "running": self.is_running(node_id),
                    "started_at": runtime.started_at,
                    "crash_count": runtime.crash_count,
                    "restarts": runtime.restarts,
                    "last_exit_code": runtime.last_exit_code,
                    "http_port": runtime.spec.http_port,
                    "proxy_port": runtime.spec.proxy_port,
                    "data_dir": runtime.spec.data_dir,
                }
            )
        return descriptions
