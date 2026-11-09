"""A disposable, network-denied perception process. Originals travel over a bounded pipe only."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

from agent_core.config import Settings
from agent_core.tools.worker.sandbox import (
    PERCEPTION_PROFILE,
    host_environment,
    perception_job_limits,
    runtime_paths,
    sandboxed,
)


class PerceptionError(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class PerceptionClient:
    def __init__(self, verified_command: tuple[str, ...] | None = None, settings: Settings | None = None) -> None:
        self.verified_command = verified_command
        self.settings = settings or Settings()
        self.limit = asyncio.Semaphore(1)

    async def run(
        self, data: bytes, corners: Any = None, rotation: int = 0, *, preview_only: bool = False
    ) -> dict[str, Any]:
        if len(data) > 20 * 1024 * 1024:
            raise PerceptionError("image_too_large")
        async with self.limit:
            with tempfile.TemporaryDirectory(prefix="jana-perception-") as scratch_name:
                scratch = Path(scratch_name).resolve()
                frozen = bool(getattr(sys, "frozen", False))
                reads = runtime_paths()
                bundled = frozen or self.verified_command is not None
                if bundled:
                    # The signed perception-pack loader must supply this command. Never execute arbitrary files
                    # discovered in packs/ or put model weights in the base installer (PLT-02/10, SEC-24).
                    if self.verified_command is None:
                        raise PerceptionError("photo_reading_unavailable")
                    command = list(self.verified_command)
                    reads = [Path(command[0]).resolve().parent]
                else:
                    command = [sys.executable, "-m", "perception_worker"]
                if not bundled:
                    spec = importlib.util.find_spec("perception_worker")
                    if spec is None or spec.origin is None:
                        raise PerceptionError("photo_reading_unavailable")
                    reads.append(Path(spec.origin).resolve().parent.parent)
                    # Development-only installed traineddata. Release bundles read only their own signed resources.
                    for root in ("/opt/homebrew/Cellar", "/opt/homebrew/share/tessdata", "/usr/local/share/tessdata"):
                        if Path(root).exists():
                            reads.append(Path(root))
                worker_env = {
                    "OMP_THREAD_LIMIT": "1",
                    "OPENBLAS_NUM_THREADS": "1",
                    "PYTHONNOUSERSITE": "1",
                    "PYTHONDONTWRITEBYTECODE": "1",
                }
                wrapped = sandboxed(command, scratch, reads=reads, limits=perception_job_limits(self.settings),
                                    profile=PERCEPTION_PROFILE, env=worker_env)  # fmt: skip
                if wrapped.reduced_protection:
                    # Perception decodes hostile binaries: no unsandboxed fallback.
                    raise PerceptionError("photo_reading_unavailable")
                argv, pass_fds = wrapped.argv, wrapped.pass_fds
                env = {"PATH": os.defpath, "HOME": str(scratch), "TMPDIR": str(scratch), **worker_env}
                if sys.platform == "win32":
                    env = host_environment(scratch)  # the launcher's; the worker's comes with its policy
                try:
                    process = await asyncio.create_subprocess_exec(
                        *argv,
                        stdin=asyncio.subprocess.PIPE,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.DEVNULL,
                        cwd=scratch,
                        env=env,
                        pass_fds=pass_fds,
                    )
                except OSError:
                    for fd in pass_fds:
                        os.close(fd)
                    raise PerceptionError("photo_reading_unavailable") from None
                for fd in pass_fds:
                    os.close(fd)
                try:
                    request = (
                        json.dumps(
                            {
                                "data": base64.b64encode(data).decode(),
                                "corners": corners,
                                "rotation": rotation,
                                "preview_only": preview_only,
                            }
                        ).encode()
                        + b"\n"
                    )
                    out, _ = await asyncio.wait_for(process.communicate(request), self.settings.perception_page_s)
                    if process.returncode != 0 or len(out) > 4_000_000:
                        raise PerceptionError("photo_reading_unavailable")
                    result = json.loads(out)
                    if "error" in result:
                        raise PerceptionError(
                            result["error"]
                            if result["error"] in ("image_too_large", "unsupported_format", "validation_failed")
                            else "photo_reading_unavailable"
                        )
                    return dict(result)
                except (ValueError, TimeoutError):
                    raise PerceptionError("photo_reading_unavailable") from None
                finally:
                    if process.returncode is None:
                        with contextlib.suppress(ProcessLookupError):
                            process.kill()
                        await process.wait()
