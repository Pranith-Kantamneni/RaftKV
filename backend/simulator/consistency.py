"""Raft safety-property checker operating on live nodes.

Each sample fetches status, log and applied history directly from every reachable node.
Checks are recorded with evidence; the checker never marks a check as passed when it did not
have enough reachable nodes to compare.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from typing import Any

import httpx


def entry_hash(raw: dict[str, Any]) -> str:
    command = raw.get("command")
    kind = raw.get("kind", "command")
    command_json = "null" if command is None else json.dumps(command, sort_keys=True, separators=(",", ":"))
    payload = f"{kind}:{command_json}"
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


class ConsistencyChecker:
    CHECKS = (
        "election_safety",
        "leader_append_only",
        "log_matching",
        "leader_completeness",
        "state_machine_safety",
        "commit_safety",
        "client_ack_safety",
    )

    def __init__(self, controller) -> None:
        self.controller = controller
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(3.0, connect=0.5))
        self.samples = 0
        self.reachable_events: list[int] = []
        self.checks: dict[str, dict[str, Any]] = {
            name: {"checked": 0, "violations": 0, "skipped": 0} for name in self.CHECKS
        }
        self.violations: list[dict[str, Any]] = []
        self.leaders_by_term: dict[int, str] = {}
        self.leader_logs: dict[tuple[str, int], dict[int, tuple[int, str]]] = {}
        self.committed_entries: dict[int, tuple[int, str]] = {}
        self.applied_entries: dict[int, str] = {}
        self.commit_state: dict[str, tuple[int, float]] = {}
        self.pending_acks: list[dict[str, Any]] = []
        self.final_convergence: dict[str, Any] = {"converged": False, "details": {}}
        self.started_at = time.time()

    async def close(self) -> None:
        await self._client.aclose()

    def _violate(self, check: str, message: str, evidence: dict[str, Any] | None = None) -> None:
        self.checks[check]["violations"] += 1
        if len(self.violations) < 100:
            self.violations.append(
                {
                    "check": check,
                    "message": message,
                    "ts": int(time.time() * 1000),
                    "evidence": evidence or {},
                }
            )

    def record_client_ack(self, node_id: str, index: int, term: int) -> None:
        self.pending_acks.append(
            {"node_id": node_id, "index": index, "term": term, "ts": time.time()}
        )

    async def sample(self) -> None:
        nodes = [n for n in self.controller.node_ids if self.controller.online.get(n)]
        self.reachable_events.append(len(nodes))
        if not nodes:
            return
        self.samples += 1
        data = await self._fetch_all(nodes)
        if len(data) < 2:
            for name in ("log_matching", "leader_completeness", "state_machine_safety"):
                self.checks[name]["skipped"] += 1
            for name in ("election_safety", "leader_append_only", "commit_safety", "client_ack_safety"):
                self._check_single(data, name)
            return

        self._check_election_safety(data)
        self._check_log_matching(data)
        self._check_leader_append_only(data)
        self._check_leader_completeness(data)
        self._check_state_machine_safety(data)
        self._check_commit_safety(data)
        self._check_client_acks(data)

    def _check_single(self, data: dict[str, Any], name: str) -> None:
        if name == "election_safety":
            self._check_election_safety(data)
        elif name == "leader_append_only":
            self._check_leader_append_only(data)
        elif name == "commit_safety":
            self._check_commit_safety(data)
        elif name == "client_ack_safety":
            self._check_client_acks(data)

    async def _fetch_all(self, nodes: list[str]) -> dict[str, Any]:
        results = await asyncio.gather(
            *(self._fetch_node(node_id) for node_id in nodes), return_exceptions=True
        )
        data: dict[str, Any] = {}
        for node_id, result in zip(nodes, results):
            if isinstance(result, BaseException) or result is None:
                continue
            data[node_id] = result
        return data

    async def _fetch_node(self, node_id: str) -> dict[str, Any] | None:
        spec = next(spec for spec in self.controller.specs if spec.node_id == node_id)
        try:
            status_response, log_response, debug_response = await asyncio.gather(
                self._client.get(f"{spec.direct_url}/raft/status"),
                self._client.get(f"{spec.direct_url}/raft/log", params={"start": 1}),
                self._client.get(f"{spec.direct_url}/raft/debug"),
            )
            status_response.raise_for_status()
            log_response.raise_for_status()
            debug_response.raise_for_status()
            return {
                "status": status_response.json(),
                "log": log_response.json(),
                "debug": debug_response.json(),
            }
        except (httpx.HTTPError, ValueError):
            return None

    # ------------------------------------------------------------------- checks
    def _check_election_safety(self, data: dict[str, Any]) -> None:
        found = 0
        for node_id, payload in data.items():
            status = payload["status"]
            if status.get("role") != "leader":
                continue
            term = int(status.get("term", 0))
            found += 1
            self.checks["election_safety"]["checked"] += 1
            previous = self.leaders_by_term.get(term)
            if previous and previous != node_id:
                self._violate(
                    "election_safety",
                    f"two leaders observed in term {term}: {previous} and {node_id}",
                    {"term": term, "nodes": [previous, node_id]},
                )
            else:
                self.leaders_by_term[term] = node_id
        if not found:
            self.checks["election_safety"]["skipped"] += 1

    def _log_entries(self, payload: dict[str, Any]) -> dict[int, tuple[int, str]]:
        entries: dict[int, tuple[int, str]] = {}
        for raw in payload["log"].get("entries", []):
            entries[int(raw["index"])] = (int(raw["term"]), entry_hash(raw))
        return entries

    def _check_log_matching(self, data: dict[str, Any]) -> None:
        nodes = list(data)
        logs = {node_id: self._log_entries(data[node_id]) for node_id in nodes}
        found = 0
        for i, left_id in enumerate(nodes):
            for right_id in nodes[i + 1 :]:
                left = logs[left_id]
                right = logs[right_id]
                common = sorted(set(left) & set(right))
                mismatches = 0
                for index in common:
                    left_term, left_hash = left[index]
                    right_term, right_hash = right[index]
                    if left_term == right_term and left_hash != right_hash:
                        mismatches += 1
                        self._violate(
                            "log_matching",
                            f"same index/term with different commands on {left_id}/{right_id}",
                            {
                                "index": index,
                                "term": left_term,
                                "left": left_hash,
                                "right": right_hash,
                            },
                        )
                    if (
                        left_term == right_term
                        and left_hash == right_hash
                        and index - 1 in left
                        and index - 1 in right
                        and left[index - 1][0] != right[index - 1][0]
                    ):
                        self._violate(
                            "log_matching",
                            f"prefix mismatch before equal entry at index {index} "
                            f"on {left_id}/{right_id}",
                            {
                                "index": index,
                                "term": left_term,
                                "previous_terms": [left[index - 1][0], right[index - 1][0]],
                            },
                        )
                    found += 1
                if common:
                    self.checks["log_matching"]["checked"] += 1
        if found == 0:
            self.checks["log_matching"]["skipped"] += 1

    def _check_leader_append_only(self, data: dict[str, Any]) -> None:
        leaders = 0
        for node_id, payload in data.items():
            status = payload["status"]
            if status.get("role") != "leader":
                continue
            leaders += 1
            term = int(status.get("term", 0))
            snapshot_index = int(payload["log"].get("snapshot_index", 0))
            current = self._log_entries(payload)
            key = (node_id, term)
            previous = self.leader_logs.get(key)
            if previous:
                self.checks["leader_append_only"]["checked"] += 1
                for index, (old_term, old_hash) in previous.items():
                    if index <= snapshot_index:
                        continue
                    new = current.get(index)
                    if new is None or new[1] != old_hash or new[0] != old_term:
                        self._violate(
                            "leader_append_only",
                            f"leader {node_id} term {term} changed/removed index {index}",
                            {
                                "node": node_id,
                                "term": term,
                                "index": index,
                                "before": [old_term, old_hash],
                                "after": list(new) if new else None,
                            },
                        )
            self.leader_logs[key] = current
        if not leaders:
            self.checks["leader_append_only"]["skipped"] += 1

    def _check_leader_completeness(self, data: dict[str, Any]) -> None:
        for node_id, payload in data.items():
            status = payload["status"]
            commit = int(status.get("commit_index", 0))
            for index, (term, digest) in self._log_entries(payload).items():
                if index <= commit:
                    self.committed_entries.setdefault(index, (term, digest))
        leaders = 0
        for node_id, payload in data.items():
            status = payload["status"]
            if status.get("role") != "leader":
                continue
            leaders += 1
            snapshot_index = int(payload["log"].get("snapshot_index", 0))
            entries = self._log_entries(payload)
            leader_term = int(status.get("term", 0))
            for index, (term, digest) in list(self.committed_entries.items()):
                if term > leader_term:
                    # An entry committed in a later term cannot be required of a leader
                    # stuck in an older term (e.g. during a partition).
                    continue
                if index <= snapshot_index:
                    continue
                present = entries.get(index)
                self.checks["leader_completeness"]["checked"] += 1
                if present is None:
                    self._violate(
                        "leader_completeness",
                        f"leader {node_id} missing committed index {index}",
                        {"node": node_id, "index": index, "term": term},
                    )
                elif present[0] != term or present[1] != digest:
                    self._violate(
                        "leader_completeness",
                        f"leader {node_id} has different committed entry at {index}",
                        {"node": node_id, "index": index, "expected": [term, digest], "actual": list(present)},
                    )
        if not leaders:
            self.checks["leader_completeness"]["skipped"] += 1

    def _check_state_machine_safety(self, data: dict[str, Any]) -> None:
        seen: dict[int, tuple[str, str]] = {}
        compared = 0
        for node_id, payload in data.items():
            for item in payload["debug"].get("applied_history", []):
                index = int(item["idx"])
                digest = item["command_hash"]
                if index in seen and seen[index][1] != digest:
                    self._violate(
                        "state_machine_safety",
                        f"applied index {index} differs between {seen[index][0]} and {node_id}",
                        {
                            "index": index,
                            "left": seen[index][1],
                            "right": digest,
                        },
                    )
                else:
                    seen.setdefault(index, (node_id, digest))
                compared += 1
        if compared:
            self.checks["state_machine_safety"]["checked"] += compared
        else:
            self.checks["state_machine_safety"]["skipped"] += 1

    def _check_commit_safety(self, data: dict[str, Any]) -> None:
        for node_id, payload in data.items():
            status = payload["status"]
            commit = int(status.get("commit_index", 0))
            applied = int(status.get("applied_index", 0))
            uptime = float(status.get("uptime_s", 0))
            previous = self.commit_state.get(node_id)
            if previous:
                prev_commit, prev_uptime = previous
                if uptime >= prev_uptime and commit < prev_commit:
                    self._violate(
                        "commit_safety",
                        f"{node_id} commit index regressed from {prev_commit} to {commit}",
                        {"node": node_id, "before": prev_commit, "after": commit},
                    )
            self.commit_state[node_id] = (commit, uptime)
            self.checks["commit_safety"]["checked"] += 1
            if applied > commit:
                self._violate(
                    "commit_safety",
                    f"{node_id} applied index {applied} exceeds commit index {commit}",
                    {"node": node_id, "applied": applied, "commit": commit},
                )
            snapshot = int(status.get("snapshot_index", 0))
            last_log = int(status.get("last_log_index", 0))
            if commit > last_log and commit > snapshot:
                self._violate(
                    "commit_safety",
                    f"{node_id} commit index {commit} exceeds last log index {last_log}",
                    {"node": node_id, "commit": commit, "last_log": last_log},
                )

    def _check_client_acks(self, data: dict[str, Any]) -> None:
        if not self.pending_acks:
            self.checks["client_ack_safety"]["skipped"] += 1
            return
        remaining = []
        for ack in self.pending_acks:
            payload = data.get(ack["node_id"])
            if time.time() - ack["ts"] < 0.75:
                # The ack may have completed after this sample's status was fetched.
                # Give polling a moment before judging it.
                remaining.append(ack)
                continue
            if not payload:
                if time.time() - ack["ts"] < 10:
                    remaining.append(ack)
                continue
            status = payload["status"]
            self.checks["client_ack_safety"]["checked"] += 1
            if int(status.get("applied_index", 0)) < ack["index"]:
                self._violate(
                    "client_ack_safety",
                    f"{ack['node_id']} acknowledged index {ack['index']} before applying it",
                    {"ack": ack, "applied": status.get("applied_index")},
                )
        self.pending_acks = remaining

    async def check_final_convergence(self, timeout: float = 15.0) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        last: dict[str, Any] = {}
        while time.monotonic() < deadline:
            nodes = [n for n in self.controller.node_ids if self.controller.online.get(n)]
            data = await self._fetch_all(nodes)
            if len(data) == len(self.controller.node_ids):
                states = {node_id: payload["debug"].get("kv", {}) for node_id, payload in data.items()}
                applied = {
                    node_id: payload["debug"].get("applied_index", 0)
                    for node_id, payload in data.items()
                }
                last = {"states": states, "applied": applied}
                reference = next(iter(states.values()))
                if all(state == reference for state in states.values()):
                    self.final_convergence = {
                        "converged": True,
                        "applied_indexes": applied,
                        "kv_count": len(reference),
                        "checked_nodes": len(data),
                    }
                    return self.final_convergence
            await asyncio.sleep(0.3)
        self.final_convergence = {
            "converged": False,
            "checked_nodes": len(last.get("states", {})),
            "applied_indexes": last.get("applied", {}),
        }
        self._violate(
            "log_matching",
            "state machines did not converge before the report deadline",
            {"detail": last.get("applied", {})},
        )
        return self.final_convergence

    def report(self) -> dict[str, Any]:
        checks = {}
        total_checked = 0
        total_violations = 0
        for name, stats in self.checks.items():
            passed = stats["violations"] == 0 and stats["checked"] > 0
            checks[name] = {
                "passed": passed,
                "checked": stats["checked"],
                "violations": stats["violations"],
                "skipped": stats["skipped"],
            }
            total_checked += stats["checked"]
            total_violations += stats["violations"]
        executed = total_checked > 0
        return {
            "passed": bool(executed and total_violations == 0),
            "samples": self.samples,
            "checks": checks,
            "violations": self.violations,
            "total_checks_executed": total_checked,
            "total_violations": total_violations,
            "final_convergence": self.final_convergence,
            "reachable_node_samples": self.reachable_events[-200:],
        }
