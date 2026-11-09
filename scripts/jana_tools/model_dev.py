"""Start the preserved Jana UI against an explicitly selected, bounded development gateway.

The gateway runs separately and owns provider credentials. This launcher neither
selects a model nor grants learner consent. Use an isolated data directory.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
from collections.abc import Mapping
from pathlib import Path

from jana_tools import REPO_ROOT


def stop(child: subprocess.Popen[bytes]) -> None:
    if child.poll() is None:
        child.terminate()
        try:
            child.wait(timeout=15)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()


def environment(base: Mapping[str, str], config: Path, data: Path, port: int, gateway_port: int) -> dict[str, str]:
    env = {key: value for key, value in base.items() if not key.upper().endswith("_API_KEY")}
    for name in (
        "GOOGLE_APPLICATION_CREDENTIALS",
        "GOOGLE_OAUTH_ACCESS_TOKEN",
        "GOOGLE_CLOUD_ACCESS_TOKEN",
        "JANA_ONLINE_DEMO",
        "JANA_META_GATEWAY_CONFIG",
        "JANA_META_GATEWAY_PORT",
    ):
        env.pop(name, None)
    env.update(
        JANA_GATEWAY_CONFIG=str(config.resolve()),
        JANA_GATEWAY_PORT=str(gateway_port),
        JANA_DEV_DATA_DIR=str(data.resolve()),
        JANA_DEV_HOST="127.0.0.1",
        PORT=str(port),
    )
    return env


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gateway-config", type=Path, required=True)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--gateway-port", type=int, default=8081)
    parser.add_argument("--port", type=int, default=5199)
    parser.add_argument("--stable", action="store_true")
    args = parser.parse_args()
    if not args.gateway_config.is_file():
        parser.error("an explicit gateway configuration file is required")
    if not all(1 <= value <= 65535 for value in (args.gateway_port, args.port)):
        parser.error("ports must be between 1 and 65535")
    pnpm = shutil.which("pnpm")
    if pnpm is None:
        parser.error("the pinned pnpm and supported Node version are required")
    env = environment(os.environ, args.gateway_config, args.data_dir, args.port, args.gateway_port)
    command = [
        pnpm,
        "--filter",
        "@jana/desktop",
        "dev",
        "--host",
        "127.0.0.1",
        "--port",
        str(args.port),
        "--strictPort",
    ]
    if args.stable:
        bundle = str(args.data_dir.resolve() / "browser-build")
        subprocess.run(
            [pnpm, "--filter", "@jana/desktop", "exec", "vite", "build", "--outDir", bundle],
            cwd=REPO_ROOT,
            env=env,
            check=True,
        )
        command = [
            pnpm,
            "--filter",
            "@jana/desktop",
            "exec",
            "vite",
            "preview",
            "--outDir",
            bundle,
            "--host",
            "127.0.0.1",
            "--port",
            str(args.port),
            "--strictPort",
        ]
    app = subprocess.Popen(command, cwd=REPO_ROOT, env=env)
    try:
        return app.wait()
    except KeyboardInterrupt:
        return 130
    finally:
        stop(app)


if __name__ == "__main__":
    raise SystemExit(main())
