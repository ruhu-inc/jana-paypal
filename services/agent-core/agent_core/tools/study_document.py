"""Disposable PDF/document extraction under the existing network-denied worker policy (STU-16)."""

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

from study_worker import DocumentError as DocumentError
from study_worker import confident_text as confident_text
from study_worker import decode as decode

from agent_core.config import Settings
from agent_core.tools.worker.sandbox import (
    PERCEPTION_PROFILE,
    host_environment,
    perception_job_limits,
    runtime_paths,
    sandboxed,
)


async def extract(data: bytes, kind: str, name: str, settings: Settings) -> list[dict[str, Any]]:
    if len(data) > settings.study_source_max_mb * 1024 * 1024:
        raise DocumentError("source_too_large")
    with tempfile.TemporaryDirectory(prefix="jana-study-") as folder:
        scratch = Path(folder).resolve()
        reads = [*runtime_paths(), Path(__file__).resolve().parents[2]]
        spec = importlib.util.find_spec("study_worker")
        if spec and spec.origin:
            reads.append(Path(spec.origin).resolve().parent.parent)
        env = {"PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1", "OMP_THREAD_LIMIT": "1"}
        command = (
            [sys.executable, "--study-document-worker"]
            if getattr(sys, "frozen", False)
            else [sys.executable, "-m", "agent_core.tools.study_document"]
        )
        wrapped = sandboxed(
            command,
            scratch,
            reads=reads,
            limits=perception_job_limits(settings),
            profile=PERCEPTION_PROFILE,
            env=env,
        )
        if wrapped.reduced_protection:
            raise DocumentError("model_unavailable")
        launch_env = (
            host_environment(scratch)
            if sys.platform == "win32"
            else {"PATH": os.defpath, "HOME": str(scratch), "TMPDIR": str(scratch), **env}
        )
        try:
            process = await asyncio.create_subprocess_exec(
                *wrapped.argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                cwd=scratch,
                pass_fds=wrapped.pass_fds,
                env=launch_env,
            )
        finally:
            for fd in wrapped.pass_fds:
                os.close(fd)
        try:
            payload = (
                json.dumps(
                    {
                        "kind": kind,
                        "name": name,
                        "data": base64.b64encode(data).decode(),
                        "max_units": settings.study_offline_max_pages,
                    }
                ).encode()
                + b"\n"
            )
            out, _ = await asyncio.wait_for(process.communicate(payload), 45)
            if process.returncode or len(out) > 4_000_000:
                raise DocumentError("processing_failed")
            result = json.loads(out)
            if "error" in result:
                raise DocumentError(result["error"])
            return list(result["units"])
        except (TimeoutError, json.JSONDecodeError):
            raise DocumentError("processing_failed") from None
        finally:
            if process.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
                await process.wait()


async def raster(data: bytes, kind: str, page: int, settings: Settings) -> bytes:
    if kind not in ("pdf", "photo"):
        raise DocumentError("unsupported_format")
    units = await extract(data, "raster_" + kind, str(page), settings)
    return base64.b64decode(units[0]["png"], validate=True)


def main() -> None:
    # The OS sandbox denies networking, process creation and file writes outside this job's scratch folder.
    if sys.platform != "win32":
        import resource

        resource.setrlimit(resource.RLIMIT_CPU, (35, 40))
        if sys.platform.startswith("linux"):
            resource.setrlimit(resource.RLIMIT_AS, (768_000_000, 768_000_000))
    result: dict[str, Any]
    try:
        raw = sys.stdin.buffer.readline(140_000_000)
        if len(raw) >= 140_000_000 or not raw.endswith(b"\n"):
            raise DocumentError("source_too_large")
        request = json.loads(raw)
        limit = request.get("max_units", 30)
        if type(limit) is not int or not 1 <= limit <= 30:
            raise DocumentError("source_too_large")
        units = decode(base64.b64decode(request["data"], validate=True), request["kind"], request["name"], limit)
        if sum(len(u["text"]) for u in units) > 2_000_000:
            raise DocumentError("source_too_large")
        result = {"units": units}
    except DocumentError as error:
        result = {"error": str(error)}
    except Exception:
        result = {"error": "corrupt_source"}
    sys.stdout.write(json.dumps(result, ensure_ascii=True) + "\n")


if __name__ == "__main__":
    main()
