"""Entrypoint for one Raft node process.

Example:
    python -m backend.raft.node_main --id n1 --port 7001 \
        --peers '{"n2": "http://127.0.0.1:7102", "n3": "http://127.0.0.1:7103"}' \
        --data-dir data/node-n1
"""

from __future__ import annotations

import argparse
import json
import logging
import os

import uvicorn

from backend.config import RaftSettings
from backend.raft.node import NodeConfig
from backend.raft.node_app import create_node_app


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a single Raft node process")
    parser.add_argument("--id", required=True, dest="node_id")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--peers", required=True, help="JSON map peer_id -> proxy URL")
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--log-level", default=os.getenv("LOG_LEVEL", "info"))
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    peers = json.loads(args.peers)
    config = NodeConfig(
        node_id=args.node_id,
        port=args.port,
        peers=peers,
        data_dir=args.data_dir,
        settings=RaftSettings.from_env(),
        seed=args.seed,
    )
    app = create_node_app(config)
    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        log_level=args.log_level,
        access_log=False,
        lifespan="on",
    )


if __name__ == "__main__":
    main()
