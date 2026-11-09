"""In-process controls of the worker (SEC-07, SEC-08), applied after the libraries are preloaded and before any
request is read. They complement the OS sandbox (SEC-06), which is the primary boundary.

- An import finder refuses every module that is not on the allow-list; importing a refused module raises
  `PolicyDenied`, which the worker reports as `policy_denied`.
- Process, network, native-call and serialisation modules that the libraries imported at load time are removed
  from the module cache and their entry points replaced, so neither our code nor a library code path can use them.
- OS resource limits: address space, open files, no core dumps, maximum file size.
"""

from __future__ import annotations

import importlib.abc
import importlib.machinery
import os
import sys
from collections.abc import Sequence
from types import ModuleType
from typing import Any

# Modules giving process, network, dynamic-code or native-call access (SEC-07).
FORBIDDEN = frozenset(
    {
        "subprocess",
        "_posixsubprocess",
        "socket",
        "_socket",
        "ssl",
        "_ssl",
        "ctypes",
        "_ctypes",
        "multiprocessing",
        "_multiprocessing",
        "pickle",
        "_pickle",
        "marshal",
        "http",
        "urllib",
        "ftplib",
        "smtplib",
        "asyncio",
        "pty",
        "webbrowser",
        "xmlrpc",
        "socketserver",
        "selectors",
        "select",
        "code",
        "codeop",
        "runpy",
        "pdb",
    }
)

# Top-level packages a call may import (SEC-07: standard library subset, SymPy, mpmath, Pint, NumPy and our
# checker packages; the generated contract runtime's dependencies per gaps.md G10).
ALLOWED = frozenset(
    {
        # our packages
        "tool_worker",
        # maths and units
        "sympy",
        "mpmath",
        "pint",
        "numpy",
        "flexcache",
        "flexparser",
        "platformdirs",
        "typing_extensions",
        # the generated contract runtime (G10)
        "pydantic",
        "pydantic_core",
        "annotated_types",
        "typing_inspection",
        "jsonschema",
        "jsonschema_specifications",
        "referencing",
        "rpds",
        "attrs",
        "attr",
        # standard library subset: pure computation, data structures and text
        "abc",
        "array",
        "ast",
        "bisect",
        "builtins",
        "cmath",
        "collections",
        "colorsys",
        "contextlib",
        "contextvars",
        "copy",
        "copyreg",
        "dataclasses",
        "datetime",
        "decimal",
        "difflib",
        "enum",
        "errno",
        "fractions",
        "functools",
        "gc",
        "hashlib",
        "heapq",
        "importlib",
        "inspect",
        "io",
        "itertools",
        "json",
        "keyword",
        "linecache",
        "locale",
        "logging",
        "math",
        "numbers",
        "operator",
        "os",
        "pathlib",
        "posixpath",
        "pprint",
        "random",
        "re",
        "reprlib",
        "resource",
        "sre_compile",
        "sre_constants",
        "sre_parse",
        "stat",
        "statistics",
        "string",
        "struct",
        "sys",
        "textwrap",
        "threading",
        "time",
        "token",
        "tokenize",
        "traceback",
        "types",
        "typing",
        "unicodedata",
        "uuid",
        "warnings",
        "weakref",
        "zlib",
        "_abc",
        "_collections",
        "_collections_abc",
        "_decimal",
        "_functools",
        "_json",
        "_operator",
        "_random",
        "_sre",
        "_stat",
        "_string",
        "_struct",
        "_thread",
        "_typing",
        "_warnings",
        "_weakref",
        "_weakrefset",
        "_bisect",
        "_heapq",
        "_datetime",
        "_pydecimal",
        "_hashlib",
        "_blake2",
        "_sha2",
        "_md5",
        "encodings",
        "codecs",
        "_codecs",
        "genericpath",
        "fnmatch",
        "glob",
        "zipimport",
        "_io",
        "_locale",
        "base64",
        "binascii",
        "calendar",
        "email",
        "quopri",
        "opcode",
        "dis",
        "_opcode",
        "platform",
        "_compat_pickle",
        "zoneinfo",
        "_strptime",
        "argparse",
        "gettext",
        "shlex",
        "tempfile",
        "shutil",
        "importlib_metadata",
        "_frozen_importlib",
        "_frozen_importlib_external",
        "_imp",
    }
)

# Entry points disabled on modules that stay loaded (os is needed by the libraries themselves).
OS_DISABLED = (
    "system",
    "popen",
    "fork",
    "forkpty",
    "execv",
    "execve",
    "execl",
    "execle",
    "execlp",
    "execlpe",
    "execvp",
    "execvpe",
    "spawnv",
    "spawnve",
    "spawnl",
    "spawnle",
    "posix_spawn",
    "posix_spawnp",
    "kill",
    "killpg",
)


class PolicyDenied(ImportError):
    """A refused import or a disabled capability (reported as policy_denied)."""


def _denied(name: str) -> Any:
    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise PolicyDenied(f"{name} is not available in the worker")

    return refuse


class AllowListFinder(importlib.abc.MetaPathFinder):
    def find_spec(
        self, fullname: str, path: Sequence[str] | None, target: ModuleType | None = None
    ) -> importlib.machinery.ModuleSpec | None:
        top = fullname.partition(".")[0]
        if top in FORBIDDEN or top not in ALLOWED:
            raise PolicyDenied(f"import of {fullname} is not allowed in the worker")
        return None  # defer to the normal finders


# Audit events refused outright (Python's runtime audit hooks: identical on every OS, SEC-06 defence in depth).
REFUSED_EVENTS = frozenset(
    {
        "socket.__new__",
        "socket.connect",
        "socket.bind",
        "socket.getaddrinfo",
        "socket.sendto",
        "subprocess.Popen",
        "os.system",
        "os.exec",
        "os.fork",
        "os.forkpty",
        "os.posix_spawn",
        "os.spawn",
        "os.kill",
        "os.killpg",
        "ctypes.dlopen",
        "ctypes.dlsym",
        "ctypes.cdata",
        "ctypes.call_function",
        "pty.spawn",
        "webbrowser.open",
    }
)
_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC


def read_roots() -> list[str]:
    """Where the worker may read: its Python runtime and environment and its own code (real paths)."""
    import sysconfig

    roots = {sys.prefix, sys.base_prefix, os.path.dirname(os.path.dirname(os.path.abspath(__file__)))}
    roots.update(sysconfig.get_paths()[k] for k in ("stdlib", "platstdlib", "purelib", "platlib"))
    return sorted({os.path.realpath(r) for r in roots})


def make_audit_hook(readable: list[str], scratch: str) -> Any:
    readable = [os.path.realpath(r) for r in readable]
    scratch = os.path.realpath(scratch)
    devices = {"/dev/null", "/dev/urandom", "/dev/random"}

    def inside(path: str, roots: list[str]) -> bool:
        real = os.path.realpath(path)
        return any(real == root or real.startswith(root + os.sep) for root in roots)

    def hook(event: str, args: tuple[Any, ...]) -> None:
        if event in REFUSED_EVENTS:
            raise PolicyDenied(f"{event} is not allowed in the worker")
        if event == "open" and args and isinstance(args[0], (str, bytes, os.PathLike)):
            path = os.fsdecode(args[0])
            mode = args[1] if len(args) > 1 and isinstance(args[1], str) else "r"
            flags = args[2] if len(args) > 2 and isinstance(args[2], int) else 0
            writing = any(c in mode for c in "wax+") or bool(flags & _WRITE_FLAGS)
            if path in devices or inside(path, [scratch]):
                return
            if writing or not inside(path, readable):
                raise PolicyDenied("file access outside the worker's runtime and scratch is not allowed")
        if event in ("os.listdir", "os.scandir") and args and isinstance(args[0], (str, bytes, os.PathLike)):
            if not inside(os.fsdecode(args[0]), [*readable, scratch]):
                raise PolicyDenied("listing directories outside the worker's runtime and scratch is not allowed")

    return hook


def install(limits: dict[str, int] | None = None, scratch: str | None = None) -> dict[str, str]:
    """Apply the in-process controls; returns what could not be applied (for the health report)."""
    problems: dict[str, str] = {}
    sys.dont_write_bytecode = True  # lazy imports must never write .pyc files outside the scratch directory
    hook = make_audit_hook(read_roots(), scratch or os.getcwd())  # before the finder: it imports sysconfig
    for name in list(sys.modules):
        if name.partition(".")[0] in FORBIDDEN:
            module = sys.modules.pop(name)
            for attribute in (
                "Popen",
                "run",
                "call",
                "check_call",
                "check_output",
                "socket",
                "create_connection",
                "getaddrinfo",
                "CDLL",
                "cdll",
                "PyDLL",
                "pydll",
                "loads",
                "load",
                "Pool",
                "Process",
            ):
                if hasattr(module, attribute):
                    setattr(module, attribute, _denied(f"{name}.{attribute}"))
    for attribute in OS_DISABLED:
        if hasattr(os, attribute):
            setattr(os, attribute, _denied(f"os.{attribute}"))
    sys.meta_path.insert(0, AllowListFinder())
    sys.addaudithook(hook)
    problems.update(set_limits(limits or {}))
    return problems


def set_limits(limits: dict[str, int]) -> dict[str, str]:
    """OS limits for the worker process (SEC-08); unsupported limits are reported, not fatal. Windows has no
    `resource` module: every OS limit is reported unsupported there and the OS sandbox and the per-call budget
    (`budget.call_budget`) remain."""
    from tool_worker.budget import enforcement

    problems = {f"per_call_{name}": detail for name, detail in enforcement().items() if detail.startswith("not")}
    try:
        import resource
    except ImportError:
        problems.update({name: "not available on this platform" for name in
                         ("RLIMIT_NOFILE", "RLIMIT_CORE", "RLIMIT_FSIZE", "RLIMIT_AS")})  # fmt: skip
        return problems
    wanted = {
        resource.RLIMIT_NOFILE: limits.get("max_open_files", 64),
        resource.RLIMIT_CORE: 0,
        resource.RLIMIT_FSIZE: limits.get("max_file_mb", 10) * 1024 * 1024,
    }
    if sys.platform.startswith("linux"):
        # Address space is enforced on Linux; macOS reserves large virtual ranges, so its memory cap is the
        # per-call limit and the supervisor's recycling.
        wanted[resource.RLIMIT_AS] = limits.get("process_memory_mb", 512) * 1024 * 1024
    else:
        problems["RLIMIT_AS"] = "address-space limit not enforced on this platform"
    for which, value in wanted.items():
        try:
            _soft, hard = resource.getrlimit(which)
            ceiling = value if hard == resource.RLIM_INFINITY else min(value, hard)
            resource.setrlimit(which, (ceiling, hard))
        except (ValueError, OSError) as error:
            problems[str(which)] = str(error)
    return problems
