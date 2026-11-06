"""Typed HTTP client for inter-node Raft RPCs."""

from __future__ import annotations

import time
from typing import Any

import httpx

from backend.raft.models import (
    AppendEntriesRequest,
    AppendEntriesResponse,
    InstallSnapshotRequest,
    InstallSnapshotResponse,
    RequestVoteRequest,
    RequestVoteResponse,
)


class RPCError(RuntimeError):
    def __init__(self, peer: str, message: str) -> None:
        super().__init__(f"RPC to {peer} failed: {message}")
        self.peer = peer


class RaftRPCClient:
    def __init__(self, node_id: str, peers: dict[str, str], timeout: float) -> None:
        self.node_id = node_id
        self.peers = peers
        self.timeout = timeout
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=min(timeout, 1.0)),
            limits=httpx.Limits(max_connections=64, max_keepalive_connections=32),
            headers={"X-Raft-From": node_id, "Content-Type": "application/json"},
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def _post(self, peer: str, path: str, payload: dict[str, Any]) -> tuple[dict[str, Any], float]:
        url = f"{self.peers[peer]}{path}"
        started = time.monotonic()
        try:
            response = await self._client.post(url, json=payload)
        except httpx.HTTPError as exc:
            raise RPCError(peer, str(exc)) from exc
        elapsed_ms = (time.monotonic() - started) * 1000
        if response.status_code >= 400:
            raise RPCError(peer, f"HTTP {response.status_code}: {response.text[:200]}")
        try:
            return response.json(), elapsed_ms
        except ValueError as exc:
            raise RPCError(peer, "invalid JSON response") from exc

    async def request_vote(
        self, peer: str, request: RequestVoteRequest
    ) -> tuple[RequestVoteResponse, float]:
        data, elapsed = await self._post(peer, "/raft/request-vote", request.model_dump())
        return RequestVoteResponse.model_validate(data), elapsed

    async def append_entries(
        self, peer: str, request: AppendEntriesRequest
    ) -> tuple[AppendEntriesResponse, float]:
        data, elapsed = await self._post(peer, "/raft/append-entries", request.model_dump())
        return AppendEntriesResponse.model_validate(data), elapsed

    async def install_snapshot(
        self, peer: str, request: InstallSnapshotRequest
    ) -> tuple[InstallSnapshotResponse, float]:
        data, elapsed = await self._post(peer, "/raft/install-snapshot", request.model_dump())
        return InstallSnapshotResponse.model_validate(data), elapsed
