"""Entry point: python -m tool_worker [--limits '<json>'].

Frames go to the real standard output; `sys.stdout` is redirected to standard error first, so a stray `print`
in any library can never corrupt the protocol stream (TOL-24: worker logs go to stderr only).

The worker is confined to its own dependency closure before any library is imported (PLT-03), so the functions, the
server and the guard are imported inside `main`."""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict

from tool_worker import closure


def main() -> None:
    closure.confine()  # only the worker's own dependency closure loads, as in the bundle (PLT-03)
    from tool_worker import guard
    from tool_worker.functions import FUNCTIONS, preload
    from tool_worker.server import Limits, Server

    parser = argparse.ArgumentParser(prog="tool_worker")
    parser.add_argument("--limits", default="{}", help="JSON object of Limits fields set by the host")
    args = parser.parse_args()
    limits = Limits(**json.loads(args.limits))
    frames = os.fdopen(os.dup(sys.stdout.fileno()), "wb", buffering=0)
    sys.stdout = sys.stderr
    preload()  # SymPy and Pint warm before the controls go on (TOL-26)
    problems = guard.install(asdict(limits))
    server = Server(FUNCTIONS, frames, limits)
    for name, detail in problems.items():
        print(f"guard: {name}: {detail}", file=sys.stderr)
    server.serve(sys.stdin.buffer)


if __name__ == "__main__":
    main()
