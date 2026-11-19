"""Entry point: ``python -m webchat``."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

if __package__ in (None, ""):  # allow `python webchat/__main__.py`
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import uvicorn

from webchat.main import create_app


def main() -> None:
    parser = argparse.ArgumentParser(description="星环 Chat demo server")
    parser.add_argument("--host", default=os.getenv("WEBCHAT_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("WEBCHAT_PORT", "8787")))
    parser.add_argument("--reload", action="store_true", help="reload on code changes")
    args = parser.parse_args()

    if args.reload:
        uvicorn.run(
            "webchat.main:create_app",
            factory=True,
            host=args.host,
            port=args.port,
            reload=True,
            reload_dirs=[str(Path(__file__).resolve().parent)],
            log_level="info",
        )
    else:
        app = create_app()
        uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
