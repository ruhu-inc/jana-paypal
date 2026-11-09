"""Cloud extraction launch policy. Hostile files never execute in the gateway process or an unconfined worker."""

from __future__ import annotations

import importlib.util
import json
import shutil
import sys
from pathlib import Path


def command(scratch: Path, override: tuple[str, ...] = ()) -> list[str]:
    argv = list(override) if override else [sys.executable, "-m", "study_worker"]
    reads = {Path(sys.base_prefix).resolve(), Path(sys.prefix).resolve(), Path(argv[0]).resolve().parent}
    for name in ("study_worker", "perception_worker"):
        spec = importlib.util.find_spec(name)
        if spec is None or spec.origin is None:
            raise RuntimeError("study_worker_unavailable")
        reads.add(Path(spec.origin).resolve().parent.parent)
    for folder in ("/opt/homebrew/Cellar", "/opt/homebrew/share/tessdata", "/usr/local/share/tessdata"):
        if Path(folder).exists():
            reads.add(Path(folder))
    if sys.platform == "darwin" and shutil.which("sandbox-exec"):
        paths = [
            *reads,
            Path("/System/Library"),
            Path("/usr/lib"),
            Path("/usr/share"),
            Path("/private/var/db/dyld"),
            Path("/System/Volumes/Preboot/Cryptexes"),
        ]
        read_rules = "\n".join("(subpath " + json.dumps(str(path)) + ")" for path in paths)
        profile = (
            "(version 1)(deny default)(deny network*)(deny process-fork)"
            "(allow process-exec (literal " + json.dumps(str(Path(argv[0]).resolve())) + "))"
            "(allow signal (target self))(allow sysctl-read)(allow process-info* (target self))"
            '(allow file-read-metadata)(allow file-read* (literal "/") (literal "/dev/urandom") '
            '(literal "/dev/null") ' + read_rules + ")"
            "(allow file-read* file-write* (subpath " + json.dumps(str(scratch)) + "))"
            '(allow file-write* (literal "/dev/null"))(allow mach-lookup '
            '(global-name "com.apple.system.opendirectoryd.libinfo"))'
        )
        return ["sandbox-exec", "-p", profile, *argv]
    if sys.platform.startswith("linux") and (bwrap := shutil.which("bwrap")):
        wrapped = [
            bwrap,
            "--unshare-all",
            "--unshare-user",
            "--disable-userns",
            "--die-with-parent",
            "--new-session",
            "--clearenv",
        ]
        paths = sorted({Path("/usr"), Path("/lib"), Path("/lib64"), Path("/bin"), *reads}, key=lambda p: len(p.parts))
        mounted: list[Path] = []
        for path in paths:
            if not any(path.is_relative_to(parent) for parent in mounted):
                wrapped += ["--ro-bind-try", str(path), str(path)]
                mounted.append(path)
        return [
            *wrapped,
            "--proc",
            "/proc",
            "--dev",
            "/dev",
            "--tmpfs",
            "/tmp",  # noqa: S108 - private tmpfs in the worker namespace
            "--bind",
            str(scratch),
            str(scratch),
            "--chdir",
            str(scratch),
            "--remount-ro",
            "/",
            "--",
            *argv,
        ]
    raise RuntimeError("study_sandbox_unavailable")
