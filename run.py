#!/usr/bin/env python
"""Start the SPADE Universal Data Optimizer.

    python run.py                     # http://127.0.0.1:8000
    python run.py --host 0.0.0.0 --port 8080
    python run.py --reload            # development

The server binds to loopback by default so a private deployment is not exposed
to the network unless you ask for it.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def main() -> int:
    parser = argparse.ArgumentParser(description="SPADE Universal Data Optimizer")
    parser.add_argument("--host", default="127.0.0.1", help="bind address (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8000, help="port (default 8000)")
    parser.add_argument("--reload", action="store_true", help="auto-reload for development")
    parser.add_argument("--log-level", default="info")
    args = parser.parse_args()

    import uvicorn

    from spade import config

    config.ensure_dirs()
    print(f"SPADE Universal Data Optimizer -> http://{args.host}:{args.port}")
    print(f"Data directory: {config.DATA_DIR}")
    uvicorn.run(
        "spade.main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level=args.log_level,
        app_dir=str(PROJECT_ROOT),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
