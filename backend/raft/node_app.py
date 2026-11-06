"""HTTP surface for a single Raft node process."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from backend.raft.models import (
    AppendEntriesRequest,
    InstallSnapshotRequest,
    RequestVoteRequest,
)
from backend.raft.node import (
    CommitTimeoutError,
    NodeConfig,
    NotLeaderError,
    QuorumUnavailableError,
    RaftNode,
)


class KVRequest(BaseModel):
    op: str = Field(pattern="^(set|delete)$")
    key: str = Field(min_length=1, max_length=256)
    value: str | None = Field(default=None, max_length=100_000)


def create_node_app(config: NodeConfig) -> FastAPI:
    node = RaftNode(config)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await node.start()
        try:
            yield
        finally:
            await node.stop()

    app = FastAPI(
        title=f"Raft node {config.node_id}",
        version="1.0.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
    )
    app.state.node = node

    @app.exception_handler(NotLeaderError)
    async def _not_leader(_: Request, exc: NotLeaderError) -> JSONResponse:
        return JSONResponse(
            status_code=409,
            content={"detail": "not leader", "leader": exc.leader_id},
        )

    @app.exception_handler(QuorumUnavailableError)
    async def _no_quorum(_: Request, exc: QuorumUnavailableError) -> JSONResponse:
        return JSONResponse(
            status_code=503,
            content={"detail": str(exc), "leader": node.leader_id},
        )

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {
            "status": "ok" if node._running else "starting",
            "node_id": node.node_id,
            "role": node.role,
            "term": node.current_term,
            "running": node._running,
        }

    @app.get("/raft/status")
    async def status() -> dict[str, Any]:
        return node.status()

    @app.get("/raft/events")
    async def events(after: int = Query(0, ge=0)) -> dict[str, Any]:
        items = node.events_since(after)
        return {"events": items, "last_seq": node.event_seq}

    @app.get("/raft/log")
    async def log_entries(
        start: int = Query(1, ge=1), end: int | None = Query(None, ge=1)
    ) -> dict[str, Any]:
        return node.log_entries(start, end)

    @app.get("/raft/debug")
    async def debug() -> dict[str, Any]:
        return node.debug_state()

    @app.post("/raft/request-vote")
    async def request_vote(request: RequestVoteRequest) -> dict[str, Any]:
        return node.handle_request_vote(request).model_dump()

    @app.post("/raft/append-entries")
    async def append_entries(request: AppendEntriesRequest) -> dict[str, Any]:
        return node.handle_append_entries(request).model_dump()

    @app.post("/raft/install-snapshot")
    async def install_snapshot(request: InstallSnapshotRequest) -> dict[str, Any]:
        return node.handle_install_snapshot(request).model_dump()

    @app.post("/kv")
    async def kv_write(body: KVRequest) -> dict[str, Any]:
        if body.op == "set":
            if body.value is None:
                raise HTTPException(status_code=422, detail="value is required for set")
            command = {"op": "set", "key": body.key, "value": body.value}
        else:
            command = {"op": "delete", "key": body.key}
        try:
            result = await node.append_command(command)
        except CommitTimeoutError as exc:
            raise HTTPException(status_code=504, detail=str(exc)) from exc
        return {
            "committed": True,
            "index": result["index"],
            "term": result["term"],
            "leader": result["leader"],
            "result": result["result"],
        }

    def _validated_key(key: str) -> str:
        if len(key) < 1 or len(key) > 256:
            raise HTTPException(status_code=422, detail="key must be 1..256 characters")
        return key

    @app.get("/kv/{key}")
    async def kv_read(key: str) -> dict[str, Any]:
        _validated_key(key)
        return await node.read_key(key)

    @app.delete("/kv/{key}")
    async def kv_delete(key: str) -> dict[str, Any]:
        _validated_key(key)
        try:
            result = await node.append_command({"op": "delete", "key": key})
        except CommitTimeoutError as exc:
            raise HTTPException(status_code=504, detail=str(exc)) from exc
        return {
            "committed": True,
            "index": result["index"],
            "term": result["term"],
            "leader": result["leader"],
            "result": result["result"],
        }

    @app.get("/kv")
    async def kv_all() -> dict[str, Any]:
        return {"items": node.kv_list(), "applied_index": node.state_machine.applied_index}

    return app
