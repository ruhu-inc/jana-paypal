"""A seccomp-BPF filter for the Linux worker sandbox (TABLE SEC-06a): no new processes (fork, vfork, clone3 and
clone without CLONE_THREAD), no ptrace and no sockets other than Unix domain. Threads stay allowed.

Under bubblewrap the program is handed over with `--seccomp FD`, and the worker's empty network namespace leaves a
Unix socket nothing to reach. The Landlock fallback (`landlock`, where bubblewrap cannot run) shares the host's
namespaces, so it installs the stricter form: no sockets at all (the worker speaks only over its pipes), signals only
to itself, and none of the calls that read or reach into another process (`process_vm_readv`, `process_vm_writev`,
`pidfd_getfd`, `pidfd_send_signal`)."""

from __future__ import annotations

import errno
import platform
import struct

# BPF instruction classes and fields.
LD_W_ABS = 0x20
JEQ_K = 0x15
JSET_K = 0x45
RET_K = 0x06
SECCOMP_RET_ALLOW = 0x7FFF0000
SECCOMP_RET_ERRNO = 0x00050000
SECCOMP_RET_KILL_PROCESS = 0x80000000
CLONE_THREAD = 0x00010000
AF_UNIX = 1
ARG0 = 16  # offsetof(struct seccomp_data, args[0]): nr, arch, instruction_pointer come first

ARCHES = {
    # machine: (AUDIT_ARCH, {syscall: number})
    "x86_64": (
        0xC000003E,
        {
            "fork": 57,
            "vfork": 58,
            "clone": 56,
            "clone3": 435,
            "ptrace": 101,
            "socket": 41,
            "kill": 62,
            "tkill": 200,
            "tgkill": 234,
            "process_vm_readv": 310,
            "process_vm_writev": 311,
            "pidfd_send_signal": 424,
            "pidfd_getfd": 438,
        },
    ),
    "aarch64": (
        0xC00000B7,
        {
            "clone": 220,
            "clone3": 435,
            "ptrace": 117,
            "socket": 198,
            "kill": 129,
            "tkill": 130,
            "tgkill": 131,
            "process_vm_readv": 270,
            "process_vm_writev": 271,
            "pidfd_send_signal": 424,
            "pidfd_getfd": 438,
        },
    ),
}
MACHINE_NAMES = {"arm64": "aarch64", "amd64": "x86_64", "AMD64": "x86_64"}  # how other systems name the same two
# Calls that reach into another process; refused outright in the Landlock form (`own_pid` given).
CROSS_PROCESS = ("tkill", "process_vm_readv", "process_vm_writev", "pidfd_send_signal", "pidfd_getfd")


def _insn(code: int, jt: int, jf: int, k: int) -> bytes:
    return struct.pack("<HBBI", code, jt, jf, k)


def program(machine: str | None = None, *, own_pid: int | None = None) -> bytes:
    """The filter. With `own_pid` (the Landlock form, where the worker shares the host's namespaces): no socket of
    any family, `kill` and `tgkill` only towards that process (the launcher's pid, which the worker keeps across its
    exec), and the cross-process calls refused."""
    arch, numbers = ARCHES[MACHINE_NAMES.get(machine or platform.machine(), machine or platform.machine())]
    deny = SECCOMP_RET_ERRNO | errno.EPERM
    insns: list[bytes] = []
    # Refuse a foreign architecture outright.
    insns += [_insn(LD_W_ABS, 0, 0, 4), _insn(JEQ_K, 1, 0, arch), _insn(RET_K, 0, 0, SECCOMP_RET_KILL_PROCESS)]
    insns.append(_insn(LD_W_ABS, 0, 0, 0))  # syscall number
    refused = ["fork", "vfork", "ptrace"] + (list(CROSS_PROCESS) if own_pid is not None else [])
    for name in refused:
        if name in numbers:
            insns += [_insn(JEQ_K, 0, 1, numbers[name]), _insn(RET_K, 0, 0, deny)]
    # clone3 hides its flags behind a pointer: answer ENOSYS so the C library falls back to clone.
    insns += [_insn(JEQ_K, 0, 1, numbers["clone3"]), _insn(RET_K, 0, 0, SECCOMP_RET_ERRNO | errno.ENOSYS)]
    # clone: allowed only for threads.
    insns += [
        _insn(JEQ_K, 0, 3, numbers["clone"]),
        _insn(LD_W_ABS, 0, 0, ARG0),  # args[0], low 32 bits (little endian)
        _insn(JSET_K, 1, 0, CLONE_THREAD),
        _insn(RET_K, 0, 0, deny),
        _insn(LD_W_ABS, 0, 0, 0),
    ]
    if own_pid is not None:
        # kill(pid, ...) and tgkill(tgid, ...): the worker itself only (raise() and abort() use tgkill on self).
        for name in ("kill", "tgkill"):
            insns += [
                _insn(JEQ_K, 0, 4, numbers[name]),
                _insn(LD_W_ABS, 0, 0, ARG0),
                _insn(JEQ_K, 1, 0, own_pid),
                _insn(RET_K, 0, 0, deny),
                _insn(LD_W_ABS, 0, 0, 0),
            ]
        insns += [_insn(JEQ_K, 0, 1, numbers["socket"]), _insn(RET_K, 0, 0, deny)]
        insns.append(_insn(RET_K, 0, 0, SECCOMP_RET_ALLOW))
        return b"".join(insns)
    # socket: Unix domain only.
    insns += [
        _insn(JEQ_K, 0, 3, numbers["socket"]),
        _insn(LD_W_ABS, 0, 0, ARG0),
        _insn(JEQ_K, 1, 0, AF_UNIX),
        _insn(RET_K, 0, 0, deny),
        _insn(RET_K, 0, 0, SECCOMP_RET_ALLOW),
    ]
    return b"".join(insns)
