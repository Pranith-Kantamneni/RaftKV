#!/usr/bin/env python3
"""Run the real benchmark suite (or one scenario) and print a readable table.

The controller must already be running, e.g.:

    make run
    .venv/bin/python scripts/benchmark.py --duration 10 --rate 6

Every number comes from the live cluster; this script only displays what the API returned.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

import httpx


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=os.getenv("RAFTKV_API", "http://127.0.0.1:8000"))
    parser.add_argument("--duration", type=float, default=10.0)
    parser.add_argument("--rate", type=float, default=6.0)
    parser.add_argument(
        "--scenario",
        choices=["normal", "follower_down", "leader_crash", "latency", "packet_loss", "partition"],
        help="Run one scenario instead of the six-scenario suite.",
    )
    parser.add_argument("--output", help="Write the raw report JSON to this path.")
    parser.add_argument("--timeout", type=float, default=900.0)
    args = parser.parse_args()

    base = args.base_url.rstrip("/")
    try:
        health = httpx.get(f"{base}/api/health", timeout=5.0)
        health.raise_for_status()
        cluster = httpx.get(f"{base}/api/cluster", timeout=5.0).json()
    except httpx.HTTPError as exc:
        print(f"controller unreachable at {base}: {exc}", file=sys.stderr)
        return 2
    if cluster.get("status") != "running":
        print("cluster is not running; start it first", file=sys.stderr)
        return 2

    print(
        f"cluster: {cluster.get('leader_id')} leader, term {cluster.get('term')}, "
        f"{cluster.get('total_nodes')} nodes"
    )
    print(f"target: {args.duration:g}s per scenario at {args.rate:g} ops/s\n")

    started = time.monotonic()
    try:
        if args.scenario:
            path = f"{base}/api/experiments/run"
            body = {"scenario": args.scenario, "duration_s": args.duration, "rate": args.rate}
            response = httpx.post(path, json=body, timeout=args.timeout)
        else:
            path = f"{base}/api/benchmarks/run"
            body = {"duration_s": args.duration, "rate": args.rate}
            response = httpx.post(path, json=body, timeout=args.timeout)
        response.raise_for_status()
        report = response.json()
    except httpx.HTTPStatusError as exc:
        detail = exc.response.text[:500]
        print(f"request failed ({exc.response.status_code}): {detail}", file=sys.stderr)
        return 1
    except httpx.HTTPError as exc:
        print(f"request failed: {exc}", file=sys.stderr)
        return 1

    elapsed = time.monotonic() - started
    if args.scenario:
        success = report.get("workload", {}).get("write_successes", 0)
        rate = success / max(0.001, report.get("workload", {}).get("measurement_window_s", 1))
        latency = report.get("workload", {}).get("latency", {})
        print(f"{'scenario':<16} {'outcome':<9} {'ops/s':>8} {'p95 ms':>9} {'fail':>6} {'consistency':>12}")
        print("-" * 68)
        print(
            f"{report.get('config', {}).get('scenario', ''):<16} "
            f"{report.get('outcome', ''):<9} "
            f"{rate:>8.2f} {latency.get('write_p95_ms', 0):>9.2f} "
            f"{report.get('workload', {}).get('write_failures', 0):>6} "
            f"{'passed' if report.get('consistency', {}).get('passed') else 'failed':>12}"
        )
    else:
        scenarios = report.get("scenarios", [])
        print(f"{'scenario':<16} {'outcome':<9} {'ops/s':>8} {'p95 ms':>9} {'fail':>6} {'recovery':>9} {'consistency':>12}")
        print("-" * 82)
        for scenario in scenarios:
            recovery = scenario.get("recovery_s")
            print(
                f"{scenario.get('scenario', ''):<16} "
                f"{scenario.get('outcome', ''):<9} "
                f"{scenario.get('throughput_ops_s', 0):>8.2f} "
                f"{scenario.get('p95_ms', 0):>9.2f} "
                f"{scenario.get('write_failures', 0):>6} "
                f"{(f'{recovery:.2f}s' if recovery is not None else '—'):>9} "
                f"{('passed' if scenario.get('consistency') else 'failed'):>12}"
            )
        print("-" * 82)
        print(f"suite: {report.get('id')} → {report.get('outcome')}")

    print(f"\nelapsed: {elapsed:.1f}s")
    if args.output:
        with open(args.output, "w", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2, sort_keys=True)
        print(f"raw report: {args.output}")
    return 0 if report.get("outcome") == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
