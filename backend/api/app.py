"""Public controller API: cluster, KV, simulation, monitoring, experiments and WebSocket."""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response

from backend.api.schemas import (
    BenchmarkRequest,
    ExperimentRequest,
    KVRequest,
    LatencyRequest,
    PartitionRequest,
    PacketLossRequest,
    RandomFailureRequest,
)
from backend.monitoring.metrics import RaftMetrics
from backend.simulator.controller import ClusterController
from backend.simulator.experiments import ExperimentRunner
from backend.simulator.processes import NodeProcessError

logger = logging.getLogger(__name__)


def create_app(controller: ClusterController | None = None) -> FastAPI:
    controller = controller or ClusterController()
    metrics = RaftMetrics()
    runner = ExperimentRunner(controller)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if controller.settings.auto_start and not controller.running:
            await controller.start()
        try:
            yield
        finally:
            if controller.running or controller.supervisor:
                await controller.shutdown()

    app = FastAPI(
        title="Raft KV Failure Simulator",
        version="1.0.0",
        lifespan=lifespan,
        description="Custom Raft implementation with real process and network fault injection.",
    )
    app.state.controller = controller
    app.state.metrics = metrics
    app.state.runner = runner
    # The dashboard is served same-origin through nginx/Vite. Cross-origin access is
    # opt-in rather than wildcard because destructive simulator endpoints have no auth.
    cors_origins = [
        origin.strip()
        for origin in os.getenv("CORS_ORIGINS", "").split(",")
        if origin.strip()
    ]
    if "*" in cors_origins:
        raise ValueError("CORS_ORIGINS must list exact origins; wildcard '*' is not allowed")
    if cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=cors_origins,
            allow_credentials=False,
            allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
            allow_headers=["Content-Type"],
        )

    @app.exception_handler(KeyError)
    async def _key_error(_: Request, exc: KeyError) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": f"not found: {exc}"})

    @app.exception_handler(ValueError)
    async def _value_error(_: Request, exc: ValueError) -> JSONResponse:
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    @app.exception_handler(NodeProcessError)
    async def _process_error(_: Request, exc: NodeProcessError) -> JSONResponse:
        return JSONResponse(status_code=500, content={"detail": str(exc)})

    @app.exception_handler(RuntimeError)
    async def _runtime_error(_: Request, exc: RuntimeError) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "controller_running": controller.running,
            "cluster_size": len(controller.node_ids),
        }

    # ------------------------------------------------------------------- cluster
    @app.get("/api/cluster")
    async def get_cluster() -> dict[str, Any]:
        return controller.cluster_snapshot()

    @app.post("/api/cluster/start")
    async def start_cluster() -> dict[str, Any]:
        if controller.running:
            return controller.cluster_snapshot()
        await controller.start()
        return controller.cluster_snapshot()

    @app.post("/api/cluster/stop")
    async def stop_cluster() -> dict[str, Any]:
        await controller.stop()
        return controller.cluster_snapshot()

    @app.get("/api/cluster/nodes")
    async def cluster_nodes() -> dict[str, Any]:
        return {"nodes": controller.cluster_snapshot()["nodes"]}

    @app.get("/api/nodes")
    async def list_nodes() -> dict[str, Any]:
        return {"nodes": controller.cluster_snapshot()["nodes"]}

    @app.get("/api/nodes/{node_id}")
    async def get_node(node_id: str) -> dict[str, Any]:
        if node_id not in controller.node_ids:
            raise HTTPException(status_code=404, detail=f"unknown node {node_id}")
        await controller.refresh_node(node_id)
        snapshot = controller.cluster_snapshot()
        node = next(n for n in snapshot["nodes"] if n["node_id"] == node_id)
        return node

    @app.get("/api/nodes/{node_id}/logs")
    async def node_logs(node_id: str, lines: int = Query(80, ge=1, le=2000)) -> dict[str, Any]:
        if node_id not in controller.node_ids or not controller.supervisor:
            raise HTTPException(status_code=404, detail=f"unknown node {node_id}")
        return {"node_id": node_id, "log": controller.supervisor.tail_log(node_id, lines)}

    @app.post("/api/nodes/{node_id}/crash")
    async def crash_node(node_id: str) -> dict[str, Any]:
        if node_id not in controller.node_ids:
            raise HTTPException(status_code=404, detail=f"unknown node {node_id}")
        await controller.crash_node(node_id)
        return controller.cluster_snapshot()

    @app.post("/api/nodes/{node_id}/restart")
    async def restart_node(node_id: str) -> dict[str, Any]:
        if node_id not in controller.node_ids:
            raise HTTPException(status_code=404, detail=f"unknown node {node_id}")
        await controller.restart_node(node_id)
        return controller.cluster_snapshot()

    # ------------------------------------------------------------------------ kv
    @app.post("/api/kv")
    async def kv_write(body: KVRequest) -> dict[str, Any]:
        if body.op == "set" and body.value is None:
            raise HTTPException(status_code=422, detail="value is required for set")
        try:
            return await controller.kv_command(body.op, body.key, body.value)
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.get("/api/kv")
    async def kv_list() -> dict[str, Any]:
        try:
            return await controller.kv_list()
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    def _validated_key(key: str) -> str:
        if len(key) < 1 or len(key) > 256:
            raise HTTPException(status_code=422, detail="key must be 1..256 characters")
        return key

    @app.get("/api/kv/{key}")
    async def kv_read(key: str) -> dict[str, Any]:
        _validated_key(key)
        try:
            return await controller.kv_get(key)
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.delete("/api/kv/{key}")
    async def kv_delete(key: str) -> dict[str, Any]:
        _validated_key(key)
        try:
            return await controller.kv_command("delete", key)
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.get("/api/log")
    async def log_view(limit: int = Query(200, ge=1, le=2000)) -> dict[str, Any]:
        try:
            return await controller.log_view(limit)
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    # ------------------------------------------------------------------ simulation
    @app.get("/api/simulation")
    async def simulation_state() -> dict[str, Any]:
        return {
            "network": controller.network.describe(),
            "random_failures": controller.random_state,
            "leader_id": controller.current_leader(),
            "online": controller.online,
        }

    @app.post("/api/simulation/partition")
    async def partition(body: PartitionRequest) -> dict[str, Any]:
        controller.set_partition(body.groups)
        return controller.cluster_snapshot()

    @app.post("/api/simulation/heal")
    async def heal() -> dict[str, Any]:
        controller.heal()
        return controller.cluster_snapshot()

    @app.post("/api/simulation/latency")
    async def latency(body: LatencyRequest) -> dict[str, Any]:
        controller.set_latency(body.latency_ms, body.source, body.target)
        return controller.cluster_snapshot()

    @app.post("/api/simulation/packet-loss")
    async def packet_loss(body: PacketLossRequest) -> dict[str, Any]:
        controller.set_loss(body.loss, body.source, body.target)
        return controller.cluster_snapshot()

    @app.post("/api/simulation/random-failures")
    async def random_failures(body: RandomFailureRequest) -> dict[str, Any]:
        await controller.start_random_failures(
            body.probability,
            body.interval_s,
            body.recovery_delay_s,
            body.max_failures,
            body.seed,
            body.duration_s,
        )
        return controller.cluster_snapshot()

    @app.post("/api/simulation/stop")
    async def stop_simulation() -> dict[str, Any]:
        await controller.stop_random_failures(restart=True)
        return controller.cluster_snapshot()

    @app.post("/api/simulation/reset")
    async def reset_simulation() -> dict[str, Any]:
        await controller.stop_random_failures(restart=True)
        controller.reset_faults()
        return controller.cluster_snapshot()

    @app.post("/api/simulation/leader-failure")
    async def leader_failure() -> dict[str, Any]:
        crashed = await controller.trigger_leader_failure()
        return {"crashed": crashed, "cluster": controller.cluster_snapshot()}

    # ----------------------------------------------------------------- monitoring
    @app.get("/api/metrics")
    async def get_metrics() -> dict[str, Any]:
        return controller.metrics_payload()

    @app.get("/api/events")
    async def get_events(
        node_id: str | None = None,
        event_type: str | None = None,
        severity: str | None = None,
        source: str | None = None,
        search: str | None = None,
        since: int | None = None,
        limit: int = Query(500, ge=1, le=2000),
    ) -> dict[str, Any]:
        return {
            "events": controller.events_payload(
                node_id=node_id,
                event_type=event_type,
                severity=severity,
                source=source,
                search=search,
                since=since,
                limit=limit,
            )
        }

    @app.get("/metrics")
    @app.get("/api/prometheus", include_in_schema=False)
    async def prometheus_metrics() -> Response:
        content, media_type = metrics.render(controller)
        return Response(content=content, media_type=media_type)

    # ---------------------------------------------------------------- experiments
    @app.get("/api/experiments")
    async def list_experiments() -> dict[str, Any]:
        return {"experiments": controller.list_experiments()}

    @app.get("/api/experiments/{experiment_id}")
    async def get_experiment(experiment_id: str) -> dict[str, Any]:
        report = controller.get_experiment(experiment_id)
        if not report:
            raise HTTPException(status_code=404, detail="experiment not found")
        return report

    @app.post("/api/experiments/run")
    async def run_experiment(body: ExperimentRequest) -> dict[str, Any]:
        try:
            return await runner.run(
                body.scenario, duration_s=body.duration_s, rate=body.rate
            )
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/benchmarks/run")
    async def run_benchmark(body: BenchmarkRequest) -> dict[str, Any]:
        try:
            return await runner.run_benchmark_suite(
                duration_s=body.duration_s, rate=body.rate
            )
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    # ----------------------------------------------------------------- websocket
    @app.websocket("/ws/cluster")
    async def cluster_socket(websocket: WebSocket) -> None:
        await websocket.accept()
        queue = controller.subscribe()
        try:
            await websocket.send_json(
                {"type": "state", "data": controller.cluster_snapshot()}
            )
            while True:
                message = await queue.get()
                await websocket.send_json(message)
        except (WebSocketDisconnect, RuntimeError):
            pass
        except Exception:  # noqa: BLE001 - client disconnected mid-write
            logger.debug("websocket closed", exc_info=True)
        finally:
            controller.unsubscribe(queue)

    return app


app = create_app()
