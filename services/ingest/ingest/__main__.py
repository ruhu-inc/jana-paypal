"""python -m ingest --config FILE [--host HOST] [--port PORT]: run the ingest.

TLS terminates in front of the service (SEC-31; the device pins our intermediate CAs). The access log is off and
proxy headers are not read, so no client address is logged or stored (TABLE OBS-19a: IP addresses are not kept)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import uvicorn

from ingest.app import create_app
from ingest.config import load_config


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="ingest")
    p.add_argument("--config", type=Path, required=True)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8081)
    args = p.parse_args(argv)
    app = create_app(load_config(args.config))
    uvicorn.run(app, host=args.host, port=args.port, access_log=False, proxy_headers=False, server_header=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
