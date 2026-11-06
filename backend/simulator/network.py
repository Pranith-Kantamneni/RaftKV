"""Real network fault injection.

Every inter-node Raft RPC is sent to the target node's proxy port. The proxy applies
partition membership, one-way latency and packet loss before forwarding over a real HTTP
socket to the target node. This is what makes injected faults affect the protocol rather
than only the UI.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

EventCallback = Callable[[dict[str, Any]], Awaitable[None] | None]


@dataclass
class LinkState:
    latency_ms: float = 0.0
    loss: float = 0.0


@dataclass
class NetworkState:
    node_ids: list[str]
    partitions: list[list[str]] = field(default_factory=list)
    links: dict[tuple[str, str], LinkState] = field(default_factory=dict)
    default_latency_ms: float = 0.0
    default_loss: float = 0.0
    rng: random.Random = field(default_factory=lambda: random.Random(7))

    def link(self, source: str, target: str) -> LinkState:
        return self.links.get(
            (source, target),
            LinkState(self.default_latency_ms, self.default_loss),
        )

    def partitioned(self, source: str, target: str) -> bool:
        if not self.partitions:
            return False
        source_group = next((i for i, g in enumerate(self.partitions) if source in g), None)
        target_group = next((i for i, g in enumerate(self.partitions) if target in g), None)
        if source_group is None or target_group is None:
            return False
        return source_group != target_group


class NetworkSimulator:
    def __init__(self, node_ids: list[str], seed: int = 7) -> None:
        self.state = NetworkState(node_ids=list(node_ids), rng=random.Random(seed))
        self.listeners: list[EventCallback] = []

    def add_listener(self, callback: EventCallback) -> None:
        self.listeners.append(callback)

    def _emit(self, event: dict[str, Any]) -> None:
        for callback in self.listeners:
            try:
                result = callback(event)
                if asyncio.iscoroutine(result):
                    asyncio.create_task(result)
            except Exception:  # noqa: BLE001
                logger.exception("network event listener failed")

    # ------------------------------------------------------------------ controls
    def set_partition(self, groups: list[list[str]]) -> None:
        known = set(self.state.node_ids)
        normalized: list[list[str]] = []
        for group in groups:
            clean = sorted(set(group))
            if not clean:
                raise ValueError("partition groups must not be empty")
            unknown = set(clean) - known
            if unknown:
                raise ValueError(f"unknown nodes in partition: {sorted(unknown)}")
            normalized.append(clean)
        union = {node for group in normalized for node in group}
        if len(normalized) < 2:
            raise ValueError("a partition needs at least two groups")
        if union != known:
            raise ValueError("partition groups must cover every node exactly once")
        self.state.partitions = normalized
        self._emit(
            {
                "ts": int(time.time() * 1000),
                "source": "network",
                "node_id": None,
                "type": "network.partitioned",
                "severity": "warning",
                "message": "network partitioned: " + " | ".join(",".join(g) for g in normalized),
                "data": {"groups": normalized},
            }
        )

    def heal(self) -> None:
        self.state.partitions = []
        self._emit(
            {
                "ts": int(time.time() * 1000),
                "source": "network",
                "node_id": None,
                "type": "network.healed",
                "severity": "info",
                "message": "network partition healed",
                "data": {},
            }
        )

    def set_latency(self, latency_ms: float, source: str | None = None, target: str | None = None) -> int:
        return self._set_link_field("latency_ms", latency_ms, source, target)

    def set_loss(self, loss: float, source: str | None = None, target: str | None = None) -> int:
        return self._set_link_field("loss", loss, source, target)

    def _set_link_field(
        self, field_name: str, value: float, source: str | None, target: str | None
    ) -> int:
        sources = [source] if source else self.state.node_ids
        targets = [target] if target else self.state.node_ids
        unknown = {s for s in sources + targets if s not in self.state.node_ids}
        if unknown:
            raise ValueError(f"unknown nodes: {sorted(unknown)}")
        changed = 0
        for src in sources:
            for dst in targets:
                if src == dst:
                    continue
                link = self.state.links.setdefault((src, dst), LinkState())
                setattr(link, field_name, value)
                changed += 1
        if source is None and target is None:
            if field_name == "latency_ms":
                self.state.default_latency_ms = value
            else:
                self.state.default_loss = value
        label = "latency" if field_name == "latency_ms" else "packet loss"
        unit = "ms" if field_name == "latency_ms" else ""
        self._emit(
            {
                "ts": int(time.time() * 1000),
                "source": "network",
                "node_id": source,
                "type": f"network.{field_name}",
                "severity": "warning" if value else "info",
                "message": f"set {label} to {value}{unit} on {changed} link(s)",
                "data": {
                    "value": value,
                    "source": source,
                    "target": target,
                    "links": changed,
                },
            }
        )
        return changed

    def reset(self) -> None:
        self.state.partitions = []
        self.state.links.clear()
        self.state.default_latency_ms = 0.0
        self.state.default_loss = 0.0
        self._emit(
            {
                "ts": int(time.time() * 1000),
                "source": "network",
                "node_id": None,
                "type": "network.reset",
                "severity": "info",
                "message": "all network faults cleared",
                "data": {},
            }
        )

    def describe(self) -> dict[str, Any]:
        return {
            "partitions": self.state.partitions,
            "default_latency_ms": self.state.default_latency_ms,
            "default_loss": self.state.default_loss,
            "links": [
                {
                    "source": src,
                    "target": dst,
                    "latency_ms": link.latency_ms,
                    "loss": link.loss,
                }
                for (src, dst), link in sorted(self.state.links.items())
                if link.latency_ms or link.loss
            ],
        }

    # ------------------------------------------------------------------- proxy
    def decide(self, source: str, target: str) -> tuple[str | None, float]:
        """Return (drop_reason, latency_ms) for a message on a link."""
        if self.state.partitioned(source, target):
            return "partition", 0.0
        link = self.state.link(source, target)
        if link.loss > 0 and self.state.rng.random() < link.loss:
            return "packet_loss", link.latency_ms
        return None, link.latency_ms


class MeshProxy:
    """HTTP proxy in front of one node that enforces the simulated link state."""

    def __init__(
        self,
        target_id: str,
        target_url: str,
        network: NetworkSimulator,
        host: str = "127.0.0.1",
        port: int = 0,
    ) -> None:
        self.target_id = target_id
        self.target_url = target_url
        self.network = network
        self.host = host
        self.port = port
        self.app = self._build_app()
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=2.0))
        self._last_heartbeat_event = 0.0
        self._server: uvicorn.Server | None = None
        self._task: asyncio.Task[Any] | None = None
        self.forwarded = 0
        self.dropped = 0

    def _build_app(self) -> FastAPI:
        app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

        @app.api_route(
            "/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD"]
        )
        async def proxy(request: Request, path: str) -> Any:
            source = request.headers.get("X-Raft-From", "controller")
            body = await request.body()
            now = time.monotonic()
            drop_reason, latency_ms = self.network.decide(source, self.target_id)
            rpc_kind = self._classify(path, body)
            if drop_reason:
                self.dropped += 1
                self._emit_network_event(
                    source,
                    rpc_kind,
                    ok=False,
                    latency_ms=latency_ms,
                    dropped=True,
                    reason=drop_reason,
                    force=True,
                )
                return JSONResponse(
                    status_code=503,
                    content={
                        "detail": f"simulated message drop ({drop_reason})",
                        "dropped": True,
                        "reason": drop_reason,
                    },
                    headers={"X-Sim-Dropped": drop_reason},
                )
            if latency_ms > 0:
                await asyncio.sleep(latency_ms / 1000.0)
            url = f"{self.target_url}/{path}"
            started = time.monotonic()
            try:
                upstream = await self._client.request(
                    request.method,
                    url,
                    params=request.query_params,
                    content=body,
                    headers={
                        "Content-Type": request.headers.get("content-type", "application/json"),
                        "X-Raft-From": source,
                    },
                )
                total_ms = (time.monotonic() - started) * 1000 + latency_ms
                self.forwarded += 1
                self._emit_network_event(
                    source,
                    rpc_kind,
                    ok=upstream.status_code < 400,
                    latency_ms=total_ms,
                    status=upstream.status_code,
                    entries=self._entry_count(body),
                )
                return JSONResponse(
                    status_code=upstream.status_code,
                    content=self._json_or_text(upstream),
                )
            except httpx.HTTPError as exc:
                self._emit_network_event(
                    source,
                    rpc_kind,
                    ok=False,
                    latency_ms=(time.monotonic() - started) * 1000 + latency_ms,
                    error=str(exc),
                    force=True,
                )
                return JSONResponse(
                    status_code=502,
                    content={"detail": f"upstream node unavailable: {exc}"},
                )

        return app

    @staticmethod
    def _json_or_text(response: httpx.Response) -> Any:
        try:
            return response.json()
        except ValueError:
            return {"detail": response.text}

    @staticmethod
    def _entry_count(body: bytes) -> int:
        if not body:
            return 0
        try:
            payload = json.loads(body)
            return len(payload.get("entries", []))
        except (ValueError, AttributeError):
            return 0

    @staticmethod
    def _classify(path: str, body: bytes) -> str:
        if path.endswith("request-vote"):
            return "RequestVote"
        if path.endswith("install-snapshot"):
            return "InstallSnapshot"
        if path.endswith("append-entries"):
            try:
                payload = json.loads(body)
                return "AppendEntries" if payload.get("entries") else "Heartbeat"
            except (ValueError, AttributeError):
                return "AppendEntries"
        return path.split("/")[-1] or "request"

    def _emit_network_event(
        self,
        source: str,
        rpc_kind: str,
        *,
        ok: bool,
        latency_ms: float,
        dropped: bool = False,
        reason: str | None = None,
        status: int | None = None,
        entries: int = 0,
        error: str | None = None,
        force: bool = False,
    ) -> None:
        if rpc_kind == "Heartbeat" and not dropped:
            now = time.monotonic()
            if not force and now - self._last_heartbeat_event < 0.9:
                return
            self._last_heartbeat_event = now
        severity = "info" if ok else "warning"
        if dropped:
            severity = "warning"
        summary = f"{source} → {self.target_id} {rpc_kind}"
        if dropped:
            summary += f" dropped ({reason})"
        elif ok:
            summary += f" ok ({latency_ms:.0f} ms)"
        else:
            summary += f" failed ({error or status})"
        self.network._emit(
            {
                "ts": int(time.time() * 1000),
                "source": "network",
                "node_id": source,
                "type": "network.message",
                "severity": severity,
                "message": summary,
                "data": {
                    "src": source,
                    "dst": self.target_id,
                    "rpc": rpc_kind,
                    "ok": ok,
                    "dropped": dropped,
                    "reason": reason,
                    "latency_ms": round(latency_ms, 2),
                    "status": status,
                    "entries": entries,
                    "error": error,
                },
            }
        )

    async def start(self) -> None:
        config = uvicorn.Config(
            self.app,
            host=self.host,
            port=self.port,
            log_level="warning",
            access_log=False,
            lifespan="off",
        )
        self._server = uvicorn.Server(config)
        self._task = asyncio.create_task(self._server.serve(), name=f"proxy-{self.target_id}")
        for _ in range(200):
            if self._server.started:
                break
            await asyncio.sleep(0.025)
        if not self._server.started:
            raise RuntimeError(f"proxy for {self.target_id} failed to start on port {self.port}")
        sockets = getattr(self._server, "servers", [])
        if sockets and sockets[0].sockets:
            self.port = sockets[0].sockets[0].getsockname()[1]
        logger.info("proxy %s -> %s listening on %s:%s", self.target_id, self.target_url, self.host, self.port)

    async def stop(self) -> None:
        if self._server:
            self._server.should_exit = True
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._task.cancel()
        await self._client.aclose()
