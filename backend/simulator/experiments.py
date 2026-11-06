"""Repeatable failure experiments and real benchmarks.

Every number in a report comes from an executed run against live node processes. The runner
injects faults while a workload is running, samples Raft safety properties, then waits for
the cluster to converge before producing the report.
"""

from __future__ import annotations

import asyncio
import logging
import platform
import random
import sys
import time
import uuid
from datetime import datetime, timezone
from typing import Any

import httpx

from backend.simulator.consistency import ConsistencyChecker

logger = logging.getLogger(__name__)

SCENARIOS = (
    "normal",
    "follower_down",
    "leader_crash",
    "latency",
    "packet_loss",
    "partition",
)

SCENARIO_DESCRIPTIONS = {
    "normal": "Three-node cluster, no faults.",
    "follower_down": "One follower is SIGKILLed, then restarted.",
    "leader_crash": "The leader is SIGKILLed; a new leader is elected; the old leader rejoins.",
    "latency": "200 ms one-way delay is injected on every inter-node link, then removed.",
    "packet_loss": "25% of inter-node messages are dropped, then loss is removed.",
    "partition": "The leader is isolated from the majority, then the partition is healed.",
}


def _pct(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return round(ordered[0], 2)
    k = (len(ordered) - 1) * p / 100.0
    lower = int(k)
    upper = min(lower + 1, len(ordered) - 1)
    return round(ordered[lower] + (ordered[upper] - ordered[lower]) * (k - lower), 2)


class ExperimentRunner:
    def __init__(self, controller) -> None:
        self.controller = controller

    async def run(
        self,
        scenario: str,
        *,
        duration_s: float = 12.0,
        rate: float = 6.0,
        kind: str = "experiment",
    ) -> dict[str, Any]:
        if scenario not in SCENARIOS:
            raise ValueError(f"unknown scenario {scenario!r}; expected one of {SCENARIOS}")
        if not 3 <= duration_s <= 120:
            raise ValueError("duration must be between 3 and 120 seconds")
        if not 0.5 <= rate <= 100:
            raise ValueError("rate must be between 0.5 and 100 operations/second")
        if not self.controller.running:
            raise RuntimeError("cluster is not running")

        async with self.controller._experiment_lock:
            return await self._run_locked(scenario, duration_s, rate, kind)

    async def _run_locked(
        self, scenario: str, duration_s: float, rate: float, kind: str
    ) -> dict[str, Any]:
        controller = self.controller
        await controller.refresh_statuses()
        leader = await controller.wait_for_leader(15)
        if not leader:
            raise RuntimeError("cannot run experiment without a leader")
        await self._ensure_all_nodes_online(timeout=15)

        experiment_id = f"{kind}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:6]}"
        started_at = time.time()
        start_commit = (controller.node_status(leader) or {}).get("commit_index", 0)
        start_seq = controller._event_seq
        checker = ConsistencyChecker(controller)
        topology = self._topology()
        config = {
            "scenario": scenario,
            "description": SCENARIO_DESCRIPTIONS[scenario],
            "duration_s": duration_s,
            "rate": rate,
            "cluster_size": len(controller.node_ids),
            "initial_leader": leader,
            "initial_commit_index": start_commit,
            "random_seed": 1234,
        }
        controller.record_event(
            "experiment.started",
            f"experiment {experiment_id} ({scenario}) started",
            data={"experiment_id": experiment_id, "scenario": scenario},
        )

        stats: dict[str, Any] = {
            "write_latencies": [],
            "read_latencies": [],
            "write_successes": 0,
            "write_failures": 0,
            "read_successes": 0,
            "read_failures": 0,
            "successful_keys": [],
            "errors": [],
            "acknowledgements": 0,
        }
        facts: dict[str, Any] = {"fault_started_at": None, "fault_ended_at": None}
        done = asyncio.Event()

        async def sampler() -> None:
            while not done.is_set():
                try:
                    await checker.sample()
                except Exception as exc:  # noqa: BLE001
                    logger.warning("consistency sample failed: %s", exc)
                try:
                    await asyncio.wait_for(done.wait(), timeout=0.5)
                except asyncio.TimeoutError:
                    pass

        sampler_task = asyncio.create_task(sampler(), name=f"checker-{experiment_id}")
        workload_task = asyncio.create_task(
            self._workload(duration_s, rate, stats, checker), name=f"workload-{experiment_id}"
        )
        fault_task = asyncio.create_task(
            self._apply_scenario(scenario, duration_s, stats, facts),
            name=f"fault-{experiment_id}",
        )
        workload_ended_at = time.time()

        try:
            await workload_task
            workload_ended_at = time.time()
            try:
                await asyncio.wait_for(fault_task, timeout=duration_s + 25)
            except asyncio.TimeoutError:
                fault_task.cancel()
                await asyncio.gather(fault_task, return_exceptions=True)
        finally:
            done.set()
            await asyncio.gather(sampler_task, return_exceptions=True)
            for task in (workload_task, fault_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(workload_task, fault_task, return_exceptions=True)

        ended_at = time.time()
        measurement_duration = max(0.001, workload_ended_at - started_at)
        actual_duration = max(0.001, ended_at - started_at)
        await controller.refresh_statuses()
        convergence = await checker.check_final_convergence(timeout=20)
        try:
            await checker.sample()
        except Exception:  # noqa: BLE001
            pass
        consistency = checker.report()
        await checker.close()

        final_leader = await controller.wait_for_leader(5)
        await controller.refresh_statuses()
        final_status = controller.node_status(final_leader) if final_leader else None
        final_commit = (final_status or {}).get("commit_index", 0)
        cluster_commits = max(0, final_commit - start_commit)

        # Verify that every acknowledged write is materialized in the final state machine.
        state_error = None
        try:
            materialized = await controller.kv_list()
            items = materialized.get("items", {})
            missing = [
                key for key in stats["successful_keys"] if key not in items
            ]
            state_error = f"{len(missing)} acknowledged keys missing from final state" if missing else None
        except Exception as exc:  # noqa: BLE001
            missing = []
            state_error = f"could not read final state: {exc}"

        write_latencies = stats["write_latencies"]
        read_latencies = stats["read_latencies"]
        total_writes = stats["write_successes"] + stats["write_failures"]
        total_reads = stats["read_successes"] + stats["read_failures"]
        assertions = self._assertions(
            scenario, facts, stats, consistency, convergence, state_error, final_leader
        )
        outcome = "passed" if all(a["passed"] for a in assertions) else "failed"

        timeline = [
            event
            for event in controller.events
            if event["ts"] >= int(started_at * 1000) - 1000
        ][-800:]
        report = {
            "id": experiment_id,
            "kind": kind,
            "created_at": int(started_at * 1000),
            "finished_at": int(ended_at * 1000),
            "config": config,
            "topology": topology,
            "hardware": self._hardware(),
            "methodology": (
                "Client workload executes unique-key SET operations (and periodic GETs) at the "
                "configured aggregate rate through the controller, which forwards only to the "
                "current Raft leader. Faults are injected at ~30% of the run and removed at ~70%. "
                "Latencies are measured client-side around the full commit round trip. Raft "
                "safety properties are sampled every 0.5 s from live node logs and state machines. "
                "No numbers are simulated or post-processed."
            ),
            "faults": facts,
            "workload": {
                "duration_s": round(actual_duration, 2),
                "measurement_window_s": round(measurement_duration, 2),
                "target_rate_ops_s": rate,
                "write_attempts": total_writes,
                "write_successes": stats["write_successes"],
                "write_failures": stats["write_failures"],
                "read_attempts": total_reads,
                "read_successes": stats["read_successes"],
                "read_failures": stats["read_failures"],
                "throughput_ops_s": round(stats["write_successes"] / measurement_duration, 2),
                "cluster_commit_throughput_ops_s": round(cluster_commits / actual_duration, 2),
                "acknowledged_keys": len(stats["successful_keys"]),
                "sample_errors": stats["errors"][:10],
                "latency": {
                    "write_avg_ms": round(sum(write_latencies) / len(write_latencies), 2)
                    if write_latencies
                    else 0.0,
                    "write_p50_ms": _pct(write_latencies, 50),
                    "write_p95_ms": _pct(write_latencies, 95),
                    "write_p99_ms": _pct(write_latencies, 99),
                    "read_avg_ms": round(sum(read_latencies) / len(read_latencies), 2)
                    if read_latencies
                    else 0.0,
                    "read_p95_ms": _pct(read_latencies, 95),
                },
            },
            "recovery": {
                "recovery_s": facts.get("recovery_s"),
                "election_ms": facts.get("election_ms"),
                "fault_window_s": (
                    round(facts["fault_ended_at"] - facts["fault_started_at"], 2)
                    if facts.get("fault_started_at") and facts.get("fault_ended_at")
                    else 0.0
                ),
                "final_leader": final_leader,
                "final_term": (final_status or {}).get("term"),
                "final_commit_index": final_commit,
                "final_applied_index": (final_status or {}).get("applied_index"),
                "cluster_commits": cluster_commits,
            },
            "consistency": consistency,
            "final_state": {
                "converged": convergence.get("converged"),
                "kv_count": convergence.get("kv_count"),
                "acknowledged_state_error": state_error,
                "applied_indexes": convergence.get("applied_indexes"),
            },
            "assertions": assertions,
            "summary": {
                "throughput_ops_s": round(stats["write_successes"] / measurement_duration, 2),
                "p95_ms": _pct(write_latencies, 95),
                "p99_ms": _pct(write_latencies, 99),
                "write_failures": stats["write_failures"],
                "recovery_s": facts.get("recovery_s"),
                "consistency": "passed" if consistency["passed"] else "failed",
            },
            "outcome": outcome,
            "event_timeline": timeline,
        }
        await controller.save_experiment(report)
        controller.record_event(
            "experiment.finished",
            f"experiment {experiment_id} finished: {outcome}",
            severity="info" if outcome == "passed" else "error",
            data={
                "experiment_id": experiment_id,
                "scenario": scenario,
                "outcome": outcome,
                "consistency": consistency["passed"],
            },
        )
        return report

    # ------------------------------------------------------------------ workload
    async def _workload(
        self,
        duration_s: float,
        rate: float,
        stats: dict[str, Any],
        checker: ConsistencyChecker,
    ) -> None:
        controller = self.controller
        workers = max(1, min(4, int(rate // 2) or 1))
        per_worker_rate = max(0.1, rate / workers)
        deadline = time.monotonic() + duration_s
        rng = random.Random(99)
        counter = 0

        async def worker(worker_id: int) -> None:
            nonlocal counter
            sequence = 0
            next_at = time.monotonic()
            read_keys: list[str] = []
            while time.monotonic() < deadline:
                counter += 1
                sequence += 1
                key = f"exp:{worker_id}:{sequence}"
                is_read = sequence % 6 == 0
                started = time.monotonic()
                if is_read:
                    target = rng.choice(read_keys) if read_keys else key
                    try:
                        result = await controller.kv_get(target)
                        latency = (time.monotonic() - started) * 1000
                        stats["read_latencies"].append(round(latency, 2))
                        stats["read_successes"] += 1
                    except Exception as exc:  # noqa: BLE001
                        stats["read_failures"] += 1
                        if len(stats["errors"]) < 20:
                            stats["errors"].append(str(exc))
                else:
                    try:
                        result = await controller.kv_command(
                            "set", key, f"value-{worker_id}-{sequence}", retries=0
                        )
                        latency = (time.monotonic() - started) * 1000
                        stats["write_latencies"].append(round(latency, 2))
                        stats["write_successes"] += 1
                        read_keys.append(key)
                        if len(read_keys) > 50:
                            read_keys.pop(0)
                        # Keep a bounded sample of keys for final-state verification.
                        if len(stats["successful_keys"]) < 200:
                            stats["successful_keys"].append(key)
                        checker.record_client_ack(
                            result["leader"], result["index"], result["term"]
                        )
                    except Exception as exc:  # noqa: BLE001
                        stats["write_failures"] += 1
                        if len(stats["errors"]) < 20:
                            stats["errors"].append(str(exc))
                next_at += 1.0 / per_worker_rate
                delay = next_at - time.monotonic()
                if delay > 0:
                    await asyncio.sleep(delay)

        await asyncio.gather(*(worker(i) for i in range(workers)))

    # ------------------------------------------------------------------- faults
    async def _apply_scenario(
        self,
        scenario: str,
        duration_s: float,
        stats: dict[str, Any],
        facts: dict[str, Any],
    ) -> None:
        controller = self.controller
        scenario_started = time.monotonic()

        async def sleep_until(fraction: float) -> None:
            target = scenario_started + duration_s * fraction
            delay = target - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)

        if scenario == "normal":
            facts["recovery_s"] = 0.0
            await asyncio.sleep(duration_s)
            return

        await sleep_until(0.3)
        facts["fault_started_at"] = time.monotonic()
        leader = controller.current_leader() or await controller.wait_for_leader(5)
        if not leader:
            facts["error"] = "no leader before fault injection"
            await sleep_until(1.0)
            return
        followers = [n for n in controller.node_ids if n != leader]

        if scenario == "follower_down":
            victim = followers[0]
            await controller.crash_node(victim, source="experiment")
            facts["victim"] = victim
            await sleep_until(0.65)
            restart_at = time.monotonic()
            await controller.restart_node(victim)
            facts["fault_ended_at"] = time.monotonic()
            await self._wait_converged(15)
            facts["recovery_s"] = round(time.monotonic() - restart_at, 2)

        elif scenario == "leader_crash":
            old_leader = leader
            crash_at = time.monotonic()
            await controller.crash_node(old_leader, source="experiment")
            facts["victim"] = old_leader
            new_leader = await controller.wait_for_leader(12, exclude={old_leader})
            facts["election_ms"] = int((time.monotonic() - crash_at) * 1000)
            facts["new_leader"] = new_leader
            facts["recovery_s"] = round((time.monotonic() - crash_at), 2)
            facts["fault_ended_at"] = time.monotonic()
            await sleep_until(0.8)
            if new_leader:
                rejoin_at = time.monotonic()
                await controller.restart_node(old_leader)
                facts["restarted"] = True
                await self._wait_converged(20)
                facts["rejoin_s"] = round(time.monotonic() - rejoin_at, 2)
            else:
                facts["restarted"] = False
                facts["rejoin_s"] = None

        elif scenario in ("latency", "packet_loss"):
            if scenario == "latency":
                controller.set_latency(200.0)
                facts["latency_ms"] = 200
            else:
                controller.set_loss(0.25)
                facts["loss"] = 0.25
            await sleep_until(0.65)
            if scenario == "latency":
                controller.set_latency(0.0)
            else:
                controller.set_loss(0.0)
            facts["fault_ended_at"] = time.monotonic()
            await self._wait_converged(10)
            facts["recovery_s"] = 0.0

        elif scenario == "partition":
            minority = [leader]
            majority = followers
            controller.set_partition([minority, majority])
            facts["groups"] = {"minority": minority, "majority": majority}
            await asyncio.sleep(2.2)
            await controller.refresh_statuses()
            old_status = controller.node_status(leader)
            old_role = (old_status or {}).get("role")
            spec = next(s for s in controller.specs if s.node_id == leader)
            rejected = False
            direct_code = None
            try:
                async with httpx.AsyncClient(timeout=8.0) as client:
                    response = await client.post(
                        f"{spec.direct_url}/kv",
                        json={"op": "set", "key": "partition:forbidden", "value": "x"},
                    )
                    direct_code = response.status_code
                    rejected = response.status_code in (409, 503, 504)
            except httpx.HTTPError as exc:
                rejected = True
                direct_code = f"transport:{type(exc).__name__}"
            facts["minority_write_rejected"] = rejected
            facts["minority_direct_status"] = direct_code
            facts["minority_role_after_partition"] = old_role
            new_leader = await controller.wait_for_leader(12, exclude={leader})
            facts["new_leader"] = new_leader
            await sleep_until(0.65)
            heal_at = time.monotonic()
            controller.heal()
            facts["fault_ended_at"] = heal_at
            await self._wait_converged(20)
            facts["recovery_s"] = round(time.monotonic() - heal_at, 2)

        if facts.get("fault_ended_at") is None:
            facts["fault_ended_at"] = time.monotonic()

    async def _wait_converged(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            await self.controller.refresh_statuses()
            online = [
                self.controller.statuses[n]
                for n in self.controller.node_ids
                if self.controller.online.get(n) and self.controller.statuses.get(n)
            ]
            if len(online) == len(self.controller.node_ids):
                leader = self.controller.current_leader_status()
                if leader:
                    target = leader.get("applied_index", 0)
                    if all(s.get("applied_index", 0) >= target for s in online):
                        return True
            await asyncio.sleep(0.25)
        return False

    async def _ensure_all_nodes_online(self, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            await self.controller.refresh_statuses()
            if all(self.controller.online.get(n) for n in self.controller.node_ids):
                return
            await asyncio.sleep(0.2)
        raise RuntimeError("not all nodes are online; cannot start a clean experiment")

    # ------------------------------------------------------------------ reports
    def _topology(self) -> dict[str, Any]:
        snapshot = self.controller.cluster_snapshot()
        return {
            "nodes": [
                {
                    "node_id": node["node_id"],
                    "http_port": node["http_port"],
                    "proxy_port": node["proxy_port"],
                    "role": node["role"],
                    "term": node["term"],
                    "leader_id": node.get("leader_id"),
                }
                for node in snapshot["nodes"]
            ],
            "leader": snapshot["leader_id"],
            "term": snapshot["term"],
            "cluster_size": snapshot["total_nodes"],
            "quorum_size": snapshot["quorum_size"],
        }

    @staticmethod
    def _hardware() -> dict[str, Any]:
        uname = platform.uname()
        return {
            "platform": uname.system,
            "release": uname.release,
            "machine": uname.machine,
            "processor": uname.processor or platform.machine(),
            "cpu_count": __import__("os").cpu_count(),
            "python": sys.version.split()[0],
        }

    def _assertions(
        self,
        scenario: str,
        facts: dict[str, Any],
        stats: dict[str, Any],
        consistency: dict[str, Any],
        convergence: dict[str, Any],
        state_error: str | None,
        final_leader: str | None,
    ) -> list[dict[str, Any]]:
        assertions: list[dict[str, Any]] = []

        def add(name: str, passed: bool, detail: str) -> None:
            assertions.append({"name": name, "passed": bool(passed), "detail": detail})

        add("raft_safety_properties", consistency["passed"], "no safety violations in sampled logs")
        add("state_machines_converged", bool(convergence.get("converged")), "all node state machines agree")
        add(
            "acknowledged_writes_persisted",
            state_error is None,
            state_error or f"{len(stats['successful_keys'])} acknowledged keys present",
        )
        add("leader_available_at_end", final_leader is not None, f"final leader: {final_leader}")
        if scenario == "normal":
            add(
                "no_write_failures",
                stats["write_failures"] == 0,
                f"{stats['write_failures']} failed writes",
            )
        elif scenario == "leader_crash":
            add(
                "new_leader_elected",
                bool(facts.get("new_leader") and facts["new_leader"] != facts.get("victim")),
                f"old={facts.get('victim')} new={facts.get('new_leader')}",
            )
            add(
                "failover_recovery_under_15s",
                facts.get("election_ms") is not None and facts["election_ms"] < 15000,
                f"election took {facts.get('election_ms')} ms",
            )
        elif scenario == "partition":
            add(
                "minority_cannot_commit",
                bool(facts.get("minority_write_rejected")),
                f"direct write to isolated node returned {facts.get('minority_direct_status')}",
            )
            add(
                "majority_elected",
                bool(
                    facts.get("new_leader")
                    and facts["new_leader"]
                    != (facts.get("groups", {}).get("minority") or [None])[0]
                ),
                f"majority leader: {facts.get('new_leader')}",
            )
        elif scenario == "follower_down":
            add(
                "recovery_completed",
                facts.get("recovery_s") is not None,
                f"recovered in {facts.get('recovery_s')} s",
            )
        elif scenario in ("latency", "packet_loss"):
            add(
                "cluster_remained_available",
                stats["write_successes"] > 0,
                f"{stats['write_successes']} successful writes under fault",
            )
        return assertions

    async def run_benchmark_suite(
        self, *, duration_s: float = 10.0, rate: float = 6.0
    ) -> dict[str, Any]:
        suite_id = f"benchmark-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}"
        reports = []
        for scenario in SCENARIOS:
            report = await self.run(
                scenario, duration_s=duration_s, rate=rate, kind="benchmark-scenario"
            )
            reports.append(report)
            if scenario == "partition":
                self.controller.reset_faults()
            await asyncio.sleep(1.0)
        comparison = []
        for report in reports:
            comparison.append(
                {
                    "scenario": report["config"]["scenario"],
                    "outcome": report["outcome"],
                    "throughput_ops_s": report["workload"]["throughput_ops_s"],
                    "p50_ms": report["workload"]["latency"]["write_p50_ms"],
                    "p95_ms": report["workload"]["latency"]["write_p95_ms"],
                    "p99_ms": report["workload"]["latency"]["write_p99_ms"],
                    "write_failures": report["workload"]["write_failures"],
                    "recovery_s": report["recovery"]["recovery_s"],
                    "consistency": report["consistency"]["passed"],
                }
            )
        suite = {
            "id": suite_id,
            "kind": "benchmark",
            "created_at": int(time.time() * 1000),
            "config": {
                "scenario": "benchmark_suite",
                "duration_s": duration_s,
                "rate": rate,
                "scenarios": list(SCENARIOS),
            },
            "hardware": self._hardware(),
            "methodology": (
                "Sequential execution of all six scenarios against the same cluster. Each "
                "scenario runs the client workload and fault injection described in its report. "
                "Reports are generated from live executions only."
            ),
            "scenarios": comparison,
            "report_ids": [report["id"] for report in reports],
            "outcome": "passed" if all(r["outcome"] == "passed" for r in reports) else "failed",
        }
        await self.controller.save_experiment(suite)
        return suite

