"""Controller/API entrypoint.

    python -m backend.main [--host 0.0.0.0] [--port 8000]
"""

from __future__ import annotations

import argparse
import logging
import os

import uvicorn

from backend.api.app import create_app


def main() -> None:
    parser = argparse.ArgumentParser(description="Raft KV simulator controller")
    parser.add_argument("--host", default=os.getenv("API_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.getenv("API_PORT", "8000")))
    parser.add_argument("--log-level", default=os.getenv("LOG_LEVEL", "info"))
    args = parser.parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    uvicorn.run(
        create_app(),
        host=args.host,
        port=args.port,
        log_level=args.log_level,
        access_log=False,
        lifespan="on",
    )


if __name__ == "__main__":
    main()
