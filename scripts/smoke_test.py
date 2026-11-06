#!/usr/bin/env python3
"""Smoke-test a running cluster: commit a write, read it back, verify all nodes converge."""

from __future__ import annotations

import argparse
import os
import sys
import time

import httpx


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=os.getenv("RAFTKV_API", "http://127.0.0.1:8000"))
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args()
    base = args.base_url.rstrip("/")
    key = f"smoke:{int(time.time())}"
    value = "committed-through-raft"

    with httpx.Client(base_url=base, timeout=10.0) as client:
        try:
            client.get("/api/health").raise_for_status()
            cluster = client.get("/api/cluster").json()
        except httpx.HTTPError as exc:
            print(f"controller unreachable: {exc}", file=sys.stderr)
            return 2
        if cluster.get("status") != "running":
            print("cluster is not running", file=sys.stderr)
            return 2
        deadline = time.monotonic() + args.timeout
        while not cluster.get("leader_id") and time.monotonic() < deadline:
            time.sleep(0.25)
            cluster = client.get("/api/cluster").json()
        leader = cluster.get("leader_id")
        if not leader:
            print("no leader elected", file=sys.stderr)
            return 1
        print(f"leader {leader}, term {cluster.get('term')}")

        try:
            write = client.post("/api/kv", json={"op": "set", "key": key, "value": value})
            write.raise_for_status()
            payload = write.json()
        except httpx.HTTPError as exc:
            print(f"write failed: {exc}", file=sys.stderr)
            return 1
        print(f"SET {key} committed at index {payload['index']} term {payload['term']}")

        try:
            read = client.get(f"/api/kv/{key}").json()
        except httpx.HTTPError as exc:
            print(f"read failed: {exc}", file=sys.stderr)
            return 1
        if not read.get("exists") or read.get("value") != value:
            print(f"read mismatch: {read}", file=sys.stderr)
            return 1
        print(f"GET {key} read_mode={read['read_mode']} value matched")

        deadline = time.monotonic() + args.timeout
        while time.monotonic() < deadline:
            listing = client.get("/api/kv").json()
            cluster = client.get("/api/cluster").json()
            indexes = {
                node["node_id"]: node["applied_index"]
                for node in cluster["nodes"]
                if node["online"]
            }
            if (
                listing.get("items", {}).get(key) == value
                and len(set(indexes.values())) == 1
                and min(indexes.values(), default=0) >= payload["index"]
            ):
                print(f"converged: applied indexes {indexes}")
                return 0
            time.sleep(0.25)
        print("state machines did not converge in time", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
