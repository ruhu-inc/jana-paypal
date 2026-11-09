"""The Linux Landlock fallback around the tool and perception workers (SEC-06, TABLE SEC-06a Linux row).

bubblewrap is the Linux sandbox. Where it cannot run (it is not installed, as with an AppImage on a distribution
without it, or unprivileged user namespaces are disabled) the worker runs under Landlock instead, which needs neither:

- File system: a ruleset handling every file access the kernel's Landlock ABI knows, granting read and execute beneath
  the worker's read paths (the runtime, its code and the system libraries) and full access beneath its scratch
  directory alone. The user's home, the learner databases and every other file stay closed.
- Network: there is no empty network namespace here, so the seccomp filter refuses every `socket()` (the worker
  speaks only over its pipes); from ABI 4 Landlock also refuses TCP bind and connect.
- Processes: the same seccomp filter refuses fork, vfork, clone without CLONE_THREAD, clone3 and ptrace, plus the calls
  that reach into another process (signals to anything but itself, `process_vm_readv` and the like); from ABI 6
  Landlock also scopes signals and abstract Unix sockets to the sandbox.

What Landlock does not hide: the names and metadata of files (stat works anywhere; open, read, list and write do not)
and System V IPC objects. bubblewrap remains the stronger mechanism and is preferred whenever it runs.

Two halves, standard library and ctypes only, like the Windows launcher:

- In the core (`abi`): the kernel's Landlock ABI version; 0 means none, and the worker then has reduced protection.
- In a small launcher (`python -m agent_core.tools.worker.landlock --policy ... -- argv`; the frozen core with
  `FROZEN_FLAG`): no_new_privs, the ruleset, `landlock_restrict_self`, the seccomp filter, then `execv` of the worker,
  which keeps the launcher's process (so the core owns the worker exactly as it owns any wrapped worker)."""

from __future__ import annotations

import base64
import ctypes
import json
import os
import shutil
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from agent_core.tools.worker import seccomp

LAUNCHER_MODULE = "agent_core.tools.worker.landlock"
FROZEN_FLAG = "--worker-landlock-launch"  # the frozen core's entry dispatches this flag to `main`

# Syscall numbers: Landlock's are the same on every architecture (asm-generic, Linux 5.13).
SYS_LANDLOCK_CREATE_RULESET = 444
SYS_LANDLOCK_ADD_RULE = 445
SYS_LANDLOCK_RESTRICT_SELF = 446
SYS_PRCTL = {"x86_64": 157, "aarch64": 167}
LANDLOCK_CREATE_RULESET_VERSION = 1 << 0
LANDLOCK_RULE_PATH_BENEATH = 1
PR_SET_NO_NEW_PRIVS = 38
PR_SET_SECCOMP = 22
SECCOMP_MODE_FILTER = 2

# File access rights (linux/landlock.h), by the ABI that introduced them.
ACCESS_FS_EXECUTE = 1 << 0
ACCESS_FS_WRITE_FILE = 1 << 1
ACCESS_FS_READ_FILE = 1 << 2
ACCESS_FS_READ_DIR = 1 << 3
ACCESS_FS_REMOVE_DIR = 1 << 4
ACCESS_FS_REMOVE_FILE = 1 << 5
ACCESS_FS_MAKE_CHAR = 1 << 6
ACCESS_FS_MAKE_DIR = 1 << 7
ACCESS_FS_MAKE_REG = 1 << 8
ACCESS_FS_MAKE_SOCK = 1 << 9
ACCESS_FS_MAKE_FIFO = 1 << 10
ACCESS_FS_MAKE_BLOCK = 1 << 11
ACCESS_FS_MAKE_SYM = 1 << 12
ACCESS_FS_REFER = 1 << 13  # ABI 2
ACCESS_FS_TRUNCATE = 1 << 14  # ABI 3
ACCESS_FS_IOCTL_DEV = 1 << 15  # ABI 5
ACCESS_NET_BIND_TCP = 1 << 0  # ABI 4
ACCESS_NET_CONNECT_TCP = 1 << 1  # ABI 4
SCOPE_ABSTRACT_UNIX_SOCKET = 1 << 0  # ABI 6
SCOPE_SIGNAL = 1 << 1  # ABI 6

FS_BY_ABI = {1: (1 << 13) - 1, 2: (1 << 14) - 1, 3: (1 << 15) - 1, 4: (1 << 15) - 1, 5: (1 << 16) - 1}
# Rights that apply to a file itself (a rule on a non-directory may grant only these).
FILE_RIGHTS = ACCESS_FS_EXECUTE | ACCESS_FS_WRITE_FILE | ACCESS_FS_READ_FILE | ACCESS_FS_TRUNCATE | ACCESS_FS_IOCTL_DEV
READ_RIGHTS = ACCESS_FS_EXECUTE | ACCESS_FS_READ_FILE | ACCESS_FS_READ_DIR
# What every Landlock-wrapped worker reads besides its own paths (the bubblewrap row binds the same system folders).
SYSTEM_READS = ("/usr", "/lib", "/lib64", "/bin", "/etc/alternatives", "/etc/ld.so.cache", "/etc/localtime",
                "/dev/urandom", "/dev/random")  # fmt: skip
SYSTEM_WRITES = ("/dev/null",)


class RulesetAttr(ctypes.Structure):
    _fields_ = [("handled_access_fs", ctypes.c_uint64), ("handled_access_net", ctypes.c_uint64),
                ("scoped", ctypes.c_uint64)]  # fmt: skip


class PathBeneathAttr(ctypes.Structure):
    _pack_ = 1  # __attribute__((packed)) in linux/landlock.h: 12 bytes
    _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32)]


class SockFilter(ctypes.Structure):
    _fields_ = [("code", ctypes.c_uint16), ("jt", ctypes.c_uint8), ("jf", ctypes.c_uint8), ("k", ctypes.c_uint32)]


class SockFprog(ctypes.Structure):
    _fields_ = [("len", ctypes.c_uint16), ("filter", ctypes.POINTER(SockFilter))]


def ruleset_size(abi_version: int) -> int:
    """The size of `struct landlock_ruleset_attr` this ABI reads: net rights from 4, scopes from 6."""
    return 24 if abi_version >= 6 else 16 if abi_version >= 4 else 8


def handled(abi_version: int) -> RulesetAttr:
    """Every right this ABI can restrict, so anything not granted by a rule is refused."""
    return RulesetAttr(
        handled_access_fs=FS_BY_ABI[min(abi_version, 5)],
        handled_access_net=(ACCESS_NET_BIND_TCP | ACCESS_NET_CONNECT_TCP) if abi_version >= 4 else 0,
        scoped=(SCOPE_ABSTRACT_UNIX_SOCKET | SCOPE_SIGNAL) if abi_version >= 6 else 0,
    )


def rule_rights(abi_version: int, wanted: int, directory: bool) -> int:
    """`wanted` as this ABI and this kind of path allow it (a file takes file rights only)."""
    rights = wanted & FS_BY_ABI[min(abi_version, 5)]
    return rights if directory else rights & FILE_RIGHTS


@dataclass(frozen=True)
class LandlockPolicy:
    read_paths: tuple[str, ...]
    write_paths: tuple[str, ...]

    def encode(self) -> str:
        document = {"read": list(self.read_paths), "write": list(self.write_paths)}
        return base64.urlsafe_b64encode(json.dumps(document, sort_keys=True).encode()).decode()

    @staticmethod
    def decode(text: str) -> LandlockPolicy:
        document = json.loads(base64.urlsafe_b64decode(text.encode()))
        return LandlockPolicy(tuple(map(str, document["read"])), tuple(map(str, document["write"])))


def policy_for(reads: Sequence[Path], scratch: Path) -> LandlockPolicy:
    return LandlockPolicy(read_paths=(*SYSTEM_READS, *(str(p) for p in reads)), write_paths=(str(scratch),
                          *SYSTEM_WRITES))  # fmt: skip


def launcher_argv(policy: LandlockPolicy, argv: Sequence[str], python: str = sys.executable,
                  frozen: bool | None = None) -> list[str]:  # fmt: skip
    """The command the core starts: the launcher (development: this module under the core's interpreter; release:
    the frozen core with `FROZEN_FLAG`), the encoded policy, then the worker's own command."""
    frozen = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
    head = [python, FROZEN_FLAG] if frozen else [python, "-m", LAUNCHER_MODULE]
    return [*head, "--policy", policy.encode(), "--", *argv]


def parse_launcher_args(args: Sequence[str]) -> tuple[LandlockPolicy, list[str]]:
    items = list(args)
    if FROZEN_FLAG in items:
        items.remove(FROZEN_FLAG)
    if len(items) < 4 or items[0] != "--policy" or items[2] != "--":
        raise ValueError("usage: --policy <encoded> -- <worker argv>")
    return LandlockPolicy.decode(items[1]), items[3:]


# --- the kernel -----------------------------------------------------------------------------------------------------


def _libc() -> ctypes.CDLL:
    return ctypes.CDLL(None, use_errno=True)


def abi(platform: str = sys.platform) -> int:
    """The kernel's Landlock ABI version, 0 where Landlock is missing, disabled or the platform is not Linux."""
    if not platform.startswith("linux"):
        return 0
    try:
        libc = _libc()
        libc.syscall.restype = ctypes.c_long
        version = libc.syscall(ctypes.c_long(SYS_LANDLOCK_CREATE_RULESET), None, ctypes.c_size_t(0),
                               ctypes.c_uint32(LANDLOCK_CREATE_RULESET_VERSION))  # fmt: skip
    except (OSError, AttributeError):
        return 0
    return max(int(version), 0)


def _check(result: int, what: str) -> int:
    if result < 0:
        number = ctypes.get_errno()
        raise OSError(number, f"{what}: {os.strerror(number)}")
    return result


def restrict(policy: LandlockPolicy, machine: str | None = None) -> None:
    """Confine this process and everything it executes to `policy`, then install the seccomp filter."""
    import platform as platform_module

    version = abi()
    if version < 1:
        raise OSError("Landlock is not available on this kernel")
    machine = machine or platform_module.machine()
    machine = seccomp.MACHINE_NAMES.get(machine, machine)
    libc = _libc()
    libc.syscall.restype = ctypes.c_long
    prctl = ctypes.c_long(SYS_PRCTL[machine])
    _check(libc.syscall(prctl, ctypes.c_long(PR_SET_NO_NEW_PRIVS), ctypes.c_ulong(1), ctypes.c_ulong(0),
                        ctypes.c_ulong(0), ctypes.c_ulong(0)), "no_new_privs")  # fmt: skip
    attr = handled(version)
    ruleset = _check(libc.syscall(ctypes.c_long(SYS_LANDLOCK_CREATE_RULESET), ctypes.byref(attr),
                                  ctypes.c_size_t(ruleset_size(version)), ctypes.c_uint32(0)), "ruleset")  # fmt: skip
    try:
        grants = [(p, READ_RIGHTS) for p in policy.read_paths] + [(p, FS_BY_ABI[5]) for p in policy.write_paths]
        for path, wanted in grants:
            try:
                fd = os.open(path, getattr(os, "O_PATH", 0o10000000) | os.O_CLOEXEC)  # O_PATH: Linux only
            except FileNotFoundError:
                continue  # a system folder this distribution does not have (/lib64 on some)
            try:
                rule = PathBeneathAttr(rule_rights(version, wanted, os.path.isdir(path)), fd)
                _check(libc.syscall(ctypes.c_long(SYS_LANDLOCK_ADD_RULE), ctypes.c_int(ruleset),
                                    ctypes.c_int(LANDLOCK_RULE_PATH_BENEATH), ctypes.byref(rule),
                                    ctypes.c_uint32(0)), f"rule for {path}")  # fmt: skip
            finally:
                os.close(fd)
        _check(libc.syscall(ctypes.c_long(SYS_LANDLOCK_RESTRICT_SELF), ctypes.c_int(ruleset), ctypes.c_uint32(0)),
               "restrict_self")  # fmt: skip
    finally:
        os.close(ruleset)
    code = seccomp.program(machine, own_pid=os.getpid())
    count = len(code) // 8
    filters = (SockFilter * count).from_buffer_copy(code)
    program = SockFprog(count, ctypes.cast(filters, ctypes.POINTER(SockFilter)))
    _check(libc.syscall(prctl, ctypes.c_long(PR_SET_SECCOMP), ctypes.c_ulong(SECCOMP_MODE_FILTER),
                        ctypes.byref(program), ctypes.c_ulong(0), ctypes.c_ulong(0)), "seccomp")  # fmt: skip


def main(args: Sequence[str] | None = None) -> int:
    """The launcher's entry. Nothing is written to stdout, which belongs to the worker's protocol."""
    try:
        policy, argv = parse_launcher_args(sys.argv[1:] if args is None else args)
        program = argv[0] if os.path.isabs(argv[0]) else shutil.which(argv[0])
        if not program:
            raise ValueError("the worker program was not found")
        restrict(policy)
        os.execv(program, argv)  # noqa: S606 (the worker's own argv, as the core built it; no shell)
    except (OSError, ValueError) as error:
        sys.stderr.write(f"worker sandbox: {error}\n")
        return 125
    return 125  # not reached: execv replaces this process


if __name__ == "__main__":
    raise SystemExit(main())
