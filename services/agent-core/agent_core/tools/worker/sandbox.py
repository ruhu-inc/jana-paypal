"""The OS sandbox around the tool worker (SEC-06, TABLE SEC-06a).

macOS: a Seatbelt profile, deny by default: no network (loopback included), reads only of the Python runtime, the
worker's code and system libraries, writes only to the per-launch scratch directory, no process creation after
start. Linux: bubblewrap with every namespace unshared (so an empty network namespace), the runtime and code bound
read-only and a private scratch directory; where bubblewrap is missing or cannot create its namespaces, the Landlock
fallback of TABLE SEC-06a (`landlock`: file rules, no sockets, no new processes). Windows: an AppContainer with no
capabilities inside a job object with kill-on-close, one active process, a memory limit and a CPU rate cap
(`winsandbox`, G16). Where the platform's sandbox cannot be applied a development build's worker runs with the
in-process controls only and reports reduced protection (the health panel shows "Checks are running with reduced
protection"); a release build's worker does not start at all (`launch.process_factory`), so checks are unavailable
rather than unsandboxed. The perception worker (D41) runs under the same sandbox with its own read paths and limits,
and never without it."""

from __future__ import annotations

import functools
import importlib.util
import os
import shutil
import subprocess
import sys
import sysconfig
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from agent_core.config import Settings
from agent_core.tools.worker import landlock, seccomp, winsandbox

TOOL_PROFILE = "Jana.ToolWorker"  # AppContainer profile names, one per worker kind (Windows)
PERCEPTION_PROFILE = "Jana.PerceptionWorker"


@dataclass(frozen=True)
class SandboxedCommand:
    argv: list[str]
    mechanism: str  # "seatbelt", "bubblewrap", "landlock", "appcontainer" or "none"
    reduced_protection: bool
    reason: str = ""
    pass_fds: tuple[int, ...] = ()  # file descriptors the sandbox launcher reads (the seccomp program)


def symlink_chain(path: Path) -> list[Path]:
    """Every path a symlink passes through on the way to its target (each must be readable in the sandbox)."""
    chain, current = [], Path(path).absolute()
    for _ in range(16):
        chain.append(current)
        if not current.is_symlink():
            break
        target = Path(os.readlink(current))
        current = target if target.is_absolute() else (current.parent / target)
    return chain


def runtime_paths(executable: str = sys.executable) -> list[Path]:
    """What the worker must read: the Python runtime, its environment and the worker's own code."""
    paths = {Path(sys.base_prefix).resolve(), Path(sys.prefix).resolve(), Path(executable).resolve().parent}
    paths.update(p.parent for p in symlink_chain(Path(executable)))
    paths.update(p.parent for p in symlink_chain(Path(sys.base_prefix)))
    for key in ("stdlib", "platstdlib", "purelib", "platlib"):
        paths.add(Path(sysconfig.get_paths()[key]).resolve())
    spec = importlib.util.find_spec("tool_worker")
    if spec and spec.origin:
        paths.add(Path(spec.origin).resolve().parent.parent)  # the worker package's source root
    # Keep only the outermost paths: a bind inside an already-bound (possibly symlinked) directory fails.
    ordered = sorted(paths, key=lambda p: len(p.parts))
    outermost: list[Path] = []
    for path in ordered:
        if not any(path.is_relative_to(kept) for kept in outermost):
            outermost.append(path)
    return sorted(outermost)


def _sbpl_string(path: Path) -> str:
    return '"' + str(path).replace("\\", "\\\\").replace('"', '\\"') + '"'


# What every Seatbelt-wrapped worker reads besides its own paths: system libraries, the dyld cache, time zone data.
SEATBELT_SYSTEM_SUBPATHS = ("/System/Library", "/usr/lib", "/usr/share", "/private/var/db/dyld",
                            "/System/Volumes/Preboot/Cryptexes")  # fmt: skip
SEATBELT_SYSTEM_LITERALS = ("/dev/null", "/dev/urandom", "/dev/random", "/private/etc/localtime")
# The root's symlinks into /private, which path resolution passes through.
SEATBELT_ROOT_LINKS = ("/var", "/etc", "/tmp")  # noqa: S108 (path names in a profile, never opened)


def metadata_literals(paths: list[Path]) -> list[Path]:
    """Every folder above the allowed paths: path resolution (realpath, the interpreter's own start) stats each one,
    so their metadata alone is readable, never their listings or anything beside them."""
    above: set[Path] = set()
    for path in paths:
        above.update(Path(path).parents)
    return sorted(above | {Path(p) for p in SEATBELT_ROOT_LINKS})


def seatbelt_profile(read_paths: list[Path], scratch: Path, executable: Path) -> str:
    reads = "\n".join(f"  (subpath {_sbpl_string(p)})" for p in read_paths)
    allowed = [*read_paths, scratch, executable, *map(Path, SEATBELT_SYSTEM_SUBPATHS),
               *map(Path, SEATBELT_SYSTEM_LITERALS)]  # fmt: skip
    metadata = "\n".join(f"  (literal {_sbpl_string(p)})" for p in metadata_literals(allowed))
    system = "\n".join(f"  (subpath {_sbpl_string(Path(p))})" for p in SEATBELT_SYSTEM_SUBPATHS)
    literals = "\n".join(f"  (literal {_sbpl_string(Path(p))})" for p in SEATBELT_SYSTEM_LITERALS)
    return f"""(version 1)
(deny default)
(deny network*)
(deny process-fork)
(allow process-exec (literal {_sbpl_string(executable)}))
(allow signal (target self))
(allow sysctl-read)
(allow process-info* (target self))
(allow file-read-metadata
{metadata})
(allow file-read*
  (literal "/")
  (literal "/dev/dtracehelper")
{reads}
{system}
{literals}
  (subpath {_sbpl_string(scratch)}))
(allow file-write*
  (subpath {_sbpl_string(scratch)})
  (literal "/dev/null")
  (literal "/dev/dtracehelper"))
(allow file-ioctl (literal "/dev/null"))
(allow mach-lookup (global-name "com.apple.system.opendirectoryd.libinfo"))
"""


def bubblewrap_argv(bwrap: str, read_paths: list[Path], scratch: Path, seccomp_fd: int | None = None,
                    env: Mapping[str, str] | None = None) -> list[str]:  # fmt: skip
    argv = [bwrap, "--unshare-all", "--die-with-parent", "--new-session", "--clearenv"]
    for system in ("/usr", "/lib", "/lib64", "/bin", "/etc/alternatives"):
        argv += ["--ro-bind-try", system, system]
    for path in read_paths:
        argv += ["--ro-bind-try", str(path), str(path)]
    argv += ["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp", "--bind", str(scratch), str(scratch)]  # noqa: S108 (a private tmpfs)
    argv += ["--setenv", "HOME", str(scratch), "--setenv", "TMPDIR", str(scratch), "--chdir", str(scratch)]
    for name, value in (env or {}).items():
        argv += ["--setenv", name, value]
    argv += ["--remount-ro", "/"]  # the sandbox's own root is read-only; only the scratch bind is writable
    if seccomp_fd is not None:
        argv += ["--seccomp", str(seccomp_fd)]
    return [*argv, "--"]


@functools.cache
def bubblewrap_usable(bwrap: str) -> bool:
    """Whether bubblewrap can create its namespaces here: it cannot where unprivileged user namespaces are disabled
    (a sysctl, or an AppArmor restriction without a profile for bwrap). Probed once per process."""
    probe = shutil.which("true") or "/bin/true"
    try:
        result = subprocess.run([bwrap, "--unshare-all", "--die-with-parent", "--ro-bind", "/", "/", probe],
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                timeout=10, check=False)  # fmt: skip
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def tool_job_limits(settings: Settings) -> winsandbox.JobLimits:
    """The tool worker's job object limits (TABLE SEC-06a Windows row, SEC-08 process memory)."""
    return winsandbox.JobLimits(memory_bytes=settings.worker_process_memory_mb * 1024 * 1024,
                                cpu_rate_percent=settings.worker_cpu_rate_percent)  # fmt: skip


def perception_job_limits(settings: Settings) -> winsandbox.JobLimits:
    """The perception worker's (SEC-08: its own memory, one page per process and a per-page time)."""
    return winsandbox.JobLimits(memory_bytes=settings.perception_memory_mb * 1024 * 1024,
                                cpu_rate_percent=settings.worker_cpu_rate_percent,
                                user_time_ms=int(settings.perception_page_s * 1000))  # fmt: skip


def windows_policy(reads: list[Path], scratch: Path, limits: winsandbox.JobLimits, profile: str,
                   env: Mapping[str, str] | None = None) -> winsandbox.WindowsPolicy:  # fmt: skip
    return winsandbox.WindowsPolicy(profile=profile, read_paths=tuple(str(p) for p in reads),
                                    write_paths=(str(scratch),), limits=limits,
                                    env={**winsandbox.worker_environment(str(scratch)), **(env or {})},
                                    cwd=str(scratch))  # fmt: skip


def host_environment(scratch: Path, platform: str = sys.platform) -> dict[str, str]:
    """The environment the core starts a (wrapped) worker with. On Windows the launcher is a Python process too and
    needs the system root to start; the worker itself gets the policy's environment."""
    env = {"PATH": "/usr/bin:/bin", "HOME": str(scratch), "TMPDIR": str(scratch), "LANG": "C.UTF-8",
           "PYTHONDONTWRITEBYTECODE": "1"}  # fmt: skip
    if platform == "win32":
        env = winsandbox.worker_environment(str(scratch))
        if "SYSTEMROOT" in env:
            env["PATH"] = str(Path(env["SYSTEMROOT"]) / "System32")
    return env


def sandboxed(argv: list[str], scratch: Path, platform: str = sys.platform, *, reads: list[Path] | None = None,
              limits: winsandbox.JobLimits | None = None, profile: str = TOOL_PROFILE,
              env: Mapping[str, str] | None = None) -> SandboxedCommand:  # fmt: skip
    """`argv` wrapped in the platform's sandbox. `reads` replaces the default read paths (the runtime and the tool
    worker's code); `limits` and `profile` are the Windows job limits and AppContainer; `env` is extra worker
    environment (bubblewrap clears the caller's; on Windows the launcher, not the caller, sets the worker's)."""
    scratch.mkdir(parents=True, exist_ok=True)
    scratch = scratch.resolve()
    reads = runtime_paths(argv[0]) if reads is None else reads
    executable = Path(argv[0]).resolve()
    if platform == "darwin" and shutil.which("sandbox-exec"):
        profile = seatbelt_profile(reads, scratch, executable)
        return SandboxedCommand(["sandbox-exec", "-p", profile, *argv], "seatbelt", False)
    if platform.startswith("linux"):
        bwrap = shutil.which("bwrap")
        if bwrap and bubblewrap_usable(bwrap):
            program_file = scratch.parent / f"{scratch.name}.seccomp"
            program_file.write_bytes(seccomp.program())
            fd = os.open(program_file, os.O_RDONLY)
            program_file.unlink()
            wrapped = [*bubblewrap_argv(bwrap, reads, scratch, fd, env), *argv]
            return SandboxedCommand(wrapped, "bubblewrap", False, pass_fds=(fd,))
        missing = "bubblewrap is not installed" if not bwrap else "bubblewrap cannot create its namespaces"
        if landlock.abi(platform) >= 1:  # TABLE SEC-06a: the Landlock fallback
            rules = landlock.policy_for(reads, scratch)
            return SandboxedCommand(landlock.launcher_argv(rules, argv), "landlock", False, missing)
        return SandboxedCommand(list(argv), "none", True, f"{missing} and Landlock is unavailable")
    if platform == "win32":
        policy = windows_policy(reads, scratch, limits or tool_job_limits(Settings()), profile, env)
        try:
            winsandbox.prepare(policy)
        except OSError as error:
            return SandboxedCommand(list(argv), "none", True, f"the Windows sandbox could not be applied: {error}")
        return SandboxedCommand(winsandbox.launcher_argv(policy, argv), "appcontainer", False)
    return SandboxedCommand(list(argv), "none", True, f"no OS sandbox for {platform}")
