"""python -m cloud_gateway --config FILE [--host HOST] [--port PORT]: run the gateway.

TLS terminates in front of the gateway (SEC-31). The access log is off and proxy headers are not read, so no client
address is logged or stored (OBS-22)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import uvicorn

from cloud_gateway.app import create_app
from cloud_gateway.config import load_deployment


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="cloud_gateway")
    p.add_argument("--config", type=Path, required=True)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8080)
    args = p.parse_args(argv)
    app = create_app(load_deployment(args.config))
    uvicorn.run(app, host=args.host, port=args.port, access_log=False, proxy_headers=False, server_header=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
