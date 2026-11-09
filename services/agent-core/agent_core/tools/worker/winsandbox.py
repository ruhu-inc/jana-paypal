"""The Windows sandbox around the tool and perception workers (SEC-06, TABLE SEC-06a Windows row; G16).

The worker runs in an AppContainer with no capabilities (so no `internetClient`, no private-network access and no
loopback), inside a job object with kill-on-close, an active-process limit of one, a memory limit and a hard CPU
rate cap. File access is what the AppContainer's ACL grants allow: read and execute on the worker's bundle (the
Python runtime and the worker's code in development), full access on the per-launch scratch directory, and whatever
the system grants every AppContainer (system libraries). The user's home, the learner databases and the keychain carry
no grant, so the worker cannot open them.

Two halves, standard library and ctypes only:

- In the core (`prepare`): the AppContainer profile is created (or found) and the ACL grants are applied. If that
  fails, `sandbox.sandboxed` reports reduced protection with the reason, as SEC-06 requires.
- In a small launcher process (`python -m agent_core.tools.worker.winsandbox --policy ... -- argv`): the job object
  is created with its limits, the worker is created suspended in the AppContainer with only its three standard
  handles inherited and child processes refused, assigned to the job and resumed. The launcher waits and exits with
  the worker's code. The core owns the launcher like any wrapped worker (sandbox-exec, bwrap): killing it closes the
  job handle, and kill-on-close ends the worker.

Everything that does not need Windows (the policy, the limits' mapping, the structure layouts, the command line and
environment block) is plain Python and tested on every platform; the calls into Windows run on the Windows runner
(`test_g16_windows_sandbox.py`)."""

from __future__ import annotations

import base64
import ctypes
import json
import os
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

# --- Windows widths, fixed so the layouts are right on every platform (c_ulong is 8 bytes on LP64) ---------------

DWORD = ctypes.c_uint32
WORD = ctypes.c_uint16
BOOL = ctypes.c_int32
HANDLE = ctypes.c_void_p
LPVOID = ctypes.c_void_p
PSID = ctypes.c_void_p
LPWSTR = ctypes.c_wchar_p
SIZE_T = ctypes.c_size_t
ULONG_PTR = ctypes.c_size_t
LARGE_INTEGER = ctypes.c_int64
ULONGLONG = ctypes.c_uint64

# Job object (winnt.h)
JOB_OBJECT_LIMIT_PROCESS_TIME = 0x00000002
JOB_OBJECT_LIMIT_ACTIVE_PROCESS = 0x00000008
JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x00000100
JOB_OBJECT_LIMIT_JOB_MEMORY = 0x00000200
JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION = 0x00000400
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JOB_OBJECT_CPU_RATE_CONTROL_ENABLE = 0x1
JOB_OBJECT_CPU_RATE_CONTROL_HARD_CAP = 0x4
JOB_OBJECT_UILIMIT_ALL = 0xFF  # handles, clipboard both ways, system parameters, display, atoms, desktop, exit
JobObjectBasicUIRestrictions = 4
JobObjectExtendedLimitInformation = 9
JobObjectCpuRateControlInformation = 15

# Process creation (winbase.h, processthreadsapi.h)
CREATE_SUSPENDED = 0x00000004
DETACHED_PROCESS = 0x00000008  # no console, so no console host process joins the one-process job
CREATE_UNICODE_ENVIRONMENT = 0x00000400
EXTENDED_STARTUPINFO_PRESENT = 0x00080000
STARTF_USESTDHANDLES = 0x00000100
HANDLE_FLAG_INHERIT = 0x00000001
PROC_THREAD_ATTRIBUTE_HANDLE_LIST = 0x00020002
PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES = 0x00020009
PROC_THREAD_ATTRIBUTE_CHILD_PROCESS_POLICY = 0x0002000E
PROCESS_CREATION_CHILD_PROCESS_RESTRICTED = 0x01
STD_HANDLES = (-10, -11, -12)  # STD_INPUT_HANDLE, STD_OUTPUT_HANDLE, STD_ERROR_HANDLE
INFINITE = 0xFFFFFFFF
WAIT_FAILED = 0xFFFFFFFF

# Access control (accctrl.h, winnt.h)
SE_FILE_OBJECT = 1
DACL_SECURITY_INFORMATION = 0x4
GRANT_ACCESS = 1
SUB_CONTAINERS_AND_OBJECTS_INHERIT = 0x3
TRUSTEE_IS_SID = 0
TRUSTEE_IS_WELL_KNOWN_GROUP = 5
FILE_GENERIC_READ_EXECUTE = 0x001200A9  # FILE_GENERIC_READ | FILE_GENERIC_EXECUTE
FILE_ALL_ACCESS = 0x001F01FF
ACCESS_ALLOWED_ACE_TYPE = 0
INHERIT_BOTH = 0x3  # OBJECT_INHERIT_ACE | CONTAINER_INHERIT_ACE
HRESULT_ALREADY_EXISTS = 0x800700B7

LAUNCHER_MODULE = "agent_core.tools.worker.winsandbox"
FROZEN_FLAG = "--worker-sandbox-launch"  # the frozen core's entry dispatches this flag to `main`


# --- the policy --------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class JobLimits:
    """The job object's limits (TABLE SEC-06a, SEC-08), from configuration keys."""

    memory_bytes: int
    cpu_rate_percent: int
    active_processes: int = 1
    user_time_ms: int | None = None  # per-process user-mode CPU time; for one-shot workers (perception)

    def __post_init__(self) -> None:
        if self.memory_bytes <= 0 or not 1 <= self.cpu_rate_percent <= 100 or self.active_processes < 1:
            raise ValueError("job limits out of range")


@dataclass(frozen=True)
class WindowsPolicy:
    """What one sandboxed launch may do. `profile` names the AppContainer (one per worker kind, so the tool worker
    never holds the perception worker's model-file grant). Paths are absolute; `write_paths` is the scratch."""

    profile: str
    read_paths: tuple[str, ...]
    write_paths: tuple[str, ...]
    limits: JobLimits
    env: dict[str, str] = field(default_factory=dict)
    cwd: str | None = None

    def encode(self) -> str:
        data = asdict(self)
        return base64.urlsafe_b64encode(json.dumps(data, sort_keys=True, separators=(",", ":")).encode()).decode()

    @classmethod
    def decode(cls, text: str) -> WindowsPolicy:
        data: dict[str, Any] = json.loads(base64.urlsafe_b64decode(text.encode()))
        return cls(
            profile=str(data["profile"]),
            read_paths=tuple(data["read_paths"]),
            write_paths=tuple(data["write_paths"]),
            limits=JobLimits(**data["limits"]),
            env=dict(data["env"]),
            cwd=data.get("cwd"),
        )


def job_limit_flags(limits: JobLimits) -> int:
    flags = (
        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        | JOB_OBJECT_LIMIT_ACTIVE_PROCESS
        | JOB_OBJECT_LIMIT_PROCESS_MEMORY
        | JOB_OBJECT_LIMIT_JOB_MEMORY
        | JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION  # no error dialog and no crash dump (SEC-08)
    )
    if limits.user_time_ms is not None:
        flags |= JOB_OBJECT_LIMIT_PROCESS_TIME
    return flags


def cpu_rate_control(limits: JobLimits) -> tuple[int, int]:
    """(ControlFlags, CpuRate): a hard cap, in hundredths of a percent of the whole machine."""
    return JOB_OBJECT_CPU_RATE_CONTROL_ENABLE | JOB_OBJECT_CPU_RATE_CONTROL_HARD_CAP, limits.cpu_rate_percent * 100


def child_command(argv: Sequence[str], executable: str = sys.executable,
                  base_executable: str | None = None) -> tuple[list[str], dict[str, str]]:  # fmt: skip
    """The command the AppContainer runs, and environment it needs. A virtual environment's `python.exe` on Windows
    is a redirector that starts the base interpreter as a child process, which the one-process job refuses; the
    base interpreter is run directly, told which environment it serves (`__PYVENV_LAUNCHER__`, as the redirector
    does)."""
    base = base_executable if base_executable is not None else getattr(sys, "_base_executable", executable)
    command = list(argv)
    if command and base and os.path.normcase(command[0]) == os.path.normcase(executable) and base != executable:
        return [base, *command[1:]], {"__PYVENV_LAUNCHER__": executable}
    return command, {}


def worker_environment(scratch: str, environ: Mapping[str, str] = os.environ) -> dict[str, str]:
    """The worker's whole environment: the scratch as home and temporary directory, and the two system variables
    the Windows runtime needs to start (cryptographic randomness and DLL search fail without SYSTEMROOT)."""
    env = {"TEMP": scratch, "TMP": scratch, "USERPROFILE": scratch, "HOME": scratch, "PYTHONDONTWRITEBYTECODE": "1",
           "PYTHONNOUSERSITE": "1", "PYTHONUTF8": "1"}  # fmt: skip
    for name in ("SYSTEMROOT", "WINDIR"):
        value = environ.get(name) or environ.get(name.capitalize())
        if value:
            env[name] = value
    return env


def environment_block(env: Mapping[str, str]) -> str:
    """A Unicode environment block: `name=value` entries sorted case-insensitively, each ended by NUL, then a NUL."""
    for name in env:
        if not name or "=" in name or "\0" in name or "\0" in env[name]:
            raise ValueError(f"invalid environment entry {name!r}")
    entries = [f"{name}={env[name]}" for name in sorted(env, key=str.upper)]
    return "\0".join(entries) + "\0\0"


def command_line(argv: Sequence[str]) -> str:
    return subprocess.list2cmdline(list(argv))


def launcher_argv(policy: WindowsPolicy, argv: Sequence[str], python: str = sys.executable,
                  frozen: bool | None = None) -> list[str]:  # fmt: skip
    """The command the core starts: the launcher (development: this module under the core's interpreter; release:
    the frozen core with `FROZEN_FLAG`), the encoded policy, then the worker's own command."""
    frozen = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
    head = [python, FROZEN_FLAG] if frozen else [python, "-m", LAUNCHER_MODULE]
    return [*head, "--policy", policy.encode(), "--", *argv]


def parse_launcher_args(args: Sequence[str]) -> tuple[WindowsPolicy, list[str]]:
    items = list(args)
    if FROZEN_FLAG in items:
        items.remove(FROZEN_FLAG)
    if len(items) < 4 or items[0] != "--policy" or items[2] != "--":
        raise ValueError("usage: --policy <encoded> -- <worker argv>")
    return WindowsPolicy.decode(items[1]), items[3:]


# --- structures (x64 layouts checked in tests) --------------------------------------------------------------------


class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", LARGE_INTEGER),
        ("PerJobUserTimeLimit", LARGE_INTEGER),
        ("LimitFlags", DWORD),
        ("MinimumWorkingSetSize", SIZE_T),
        ("MaximumWorkingSetSize", SIZE_T),
        ("ActiveProcessLimit", DWORD),
        ("Affinity", ULONG_PTR),
        ("PriorityClass", DWORD),
        ("SchedulingClass", DWORD),
    ]


class IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        (name, ULONGLONG)
        for name in ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount", "ReadTransferCount",
                     "WriteTransferCount", "OtherTransferCount")
    ]  # fmt: skip


class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", IO_COUNTERS),
        ("ProcessMemoryLimit", SIZE_T),
        ("JobMemoryLimit", SIZE_T),
        ("PeakProcessMemoryUsed", SIZE_T),
        ("PeakJobMemoryUsed", SIZE_T),
    ]


class JOBOBJECT_CPU_RATE_CONTROL_INFORMATION(ctypes.Structure):
    _fields_ = [("ControlFlags", DWORD), ("CpuRate", DWORD)]  # CpuRate is the first member of a 4-byte union


class JOBOBJECT_BASIC_UI_RESTRICTIONS(ctypes.Structure):
    _fields_ = [("UIRestrictionsClass", DWORD)]


class SECURITY_CAPABILITIES(ctypes.Structure):
    _fields_ = [("AppContainerSid", PSID), ("Capabilities", LPVOID), ("CapabilityCount", DWORD), ("Reserved", DWORD)]


class STARTUPINFOW(ctypes.Structure):
    _fields_ = [
        ("cb", DWORD),
        ("lpReserved", LPWSTR),
        ("lpDesktop", LPWSTR),
        ("lpTitle", LPWSTR),
        ("dwX", DWORD),
        ("dwY", DWORD),
        ("dwXSize", DWORD),
        ("dwYSize", DWORD),
        ("dwXCountChars", DWORD),
        ("dwYCountChars", DWORD),
        ("dwFillAttribute", DWORD),
        ("dwFlags", DWORD),
        ("wShowWindow", WORD),
        ("cbReserved2", WORD),
        ("lpReserved2", LPVOID),
        ("hStdInput", HANDLE),
        ("hStdOutput", HANDLE),
        ("hStdError", HANDLE),
    ]


class STARTUPINFOEXW(ctypes.Structure):
    _fields_ = [("StartupInfo", STARTUPINFOW), ("lpAttributeList", LPVOID)]


class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [("hProcess", HANDLE), ("hThread", HANDLE), ("dwProcessId", DWORD), ("dwThreadId", DWORD)]


class TRUSTEE_W(ctypes.Structure):
    _fields_ = [
        ("pMultipleTrustee", LPVOID),
        ("MultipleTrusteeOperation", DWORD),
        ("TrusteeForm", DWORD),
        ("TrusteeType", DWORD),
        ("ptstrName", LPVOID),  # a PSID when TrusteeForm is TRUSTEE_IS_SID
    ]


class ACL(ctypes.Structure):
    _fields_ = [("AclRevision", ctypes.c_uint8), ("Sbz1", ctypes.c_uint8), ("AclSize", WORD), ("AceCount", WORD),
                ("Sbz2", WORD)]  # fmt: skip


class ACE_HEADER(ctypes.Structure):
    _fields_ = [("AceType", ctypes.c_uint8), ("AceFlags", ctypes.c_uint8), ("AceSize", WORD)]


class ACCESS_ALLOWED_ACE(ctypes.Structure):
    _fields_ = [("Header", ACE_HEADER), ("Mask", DWORD), ("SidStart", DWORD)]  # the SID starts at SidStart


class EXPLICIT_ACCESS_W(ctypes.Structure):
    _fields_ = [("grfAccessPermissions", DWORD), ("grfAccessMode", DWORD), ("grfInheritance", DWORD),
                ("Trustee", TRUSTEE_W)]  # fmt: skip


def extended_limits(limits: JobLimits) -> JOBOBJECT_EXTENDED_LIMIT_INFORMATION:
    info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    info.BasicLimitInformation.LimitFlags = job_limit_flags(limits)
    info.BasicLimitInformation.ActiveProcessLimit = limits.active_processes
    if limits.user_time_ms is not None:
        info.BasicLimitInformation.PerProcessUserTimeLimit = limits.user_time_ms * 10_000  # 100 ns units
    info.ProcessMemoryLimit = limits.memory_bytes
    info.JobMemoryLimit = limits.memory_bytes
    return info


def explicit_access(sid: int | None, access: int) -> EXPLICIT_ACCESS_W:
    entry = EXPLICIT_ACCESS_W()
    entry.grfAccessPermissions = access
    entry.grfAccessMode = GRANT_ACCESS
    entry.grfInheritance = SUB_CONTAINERS_AND_OBJECTS_INHERIT
    entry.Trustee.TrusteeForm = TRUSTEE_IS_SID
    entry.Trustee.TrusteeType = TRUSTEE_IS_WELL_KNOWN_GROUP
    entry.Trustee.ptstrName = sid
    return entry


# --- the Windows calls ----------------------------------------------------------------------------------------------


def on_windows() -> bool:
    return sys.platform == "win32"


class SandboxUnavailable(OSError):
    """The AppContainer or the job object could not be set up; the caller reports reduced protection."""


class _Api:
    """kernel32, advapi32 and userenv with argument types declared, loaded only on Windows."""

    kernel32: Any
    advapi32: Any
    userenv: Any

    def __init__(self) -> None:
        if not on_windows():  # a call, not a platform check, so the type checker keeps the rest reachable
            raise SandboxUnavailable("the Windows sandbox runs on Windows only")
        windll: Any = getattr(ctypes, "WinDLL")  # noqa: B009 (absent from ctypes off Windows)
        self.kernel32 = windll("kernel32", use_last_error=True)
        self.advapi32 = windll("advapi32", use_last_error=True)
        try:
            self.userenv = windll("userenv", use_last_error=True)
            self.userenv.CreateAppContainerProfile  # noqa: B018 (Windows 8 and later)
        except (OSError, AttributeError) as error:
            raise SandboxUnavailable("AppContainers need Windows 8 or later") from error
        k, a, u = self.kernel32, self.advapi32, self.userenv
        ptr = ctypes.POINTER
        u.CreateAppContainerProfile.argtypes = [LPWSTR, LPWSTR, LPWSTR, LPVOID, DWORD, ptr(PSID)]
        u.CreateAppContainerProfile.restype = ctypes.c_int32
        u.DeriveAppContainerSidFromAppContainerName.argtypes = [LPWSTR, ptr(PSID)]
        u.DeriveAppContainerSidFromAppContainerName.restype = ctypes.c_int32
        a.FreeSid.argtypes = [PSID]
        a.FreeSid.restype = LPVOID
        a.GetNamedSecurityInfoW.argtypes = [LPWSTR, DWORD, DWORD, LPVOID, LPVOID, ptr(LPVOID), LPVOID, ptr(LPVOID)]
        a.GetNamedSecurityInfoW.restype = DWORD
        a.GetAce.argtypes = [LPVOID, DWORD, ptr(LPVOID)]
        a.GetAce.restype = BOOL
        a.EqualSid.argtypes = [PSID, PSID]
        a.EqualSid.restype = BOOL
        a.SetEntriesInAclW.argtypes = [DWORD, ptr(EXPLICIT_ACCESS_W), LPVOID, ptr(LPVOID)]
        a.SetEntriesInAclW.restype = DWORD
        a.SetNamedSecurityInfoW.argtypes = [LPWSTR, DWORD, DWORD, LPVOID, LPVOID, LPVOID, LPVOID]
        a.SetNamedSecurityInfoW.restype = DWORD
        k.LocalFree.argtypes = [LPVOID]
        k.LocalFree.restype = LPVOID
        k.CreateJobObjectW.argtypes = [LPVOID, LPWSTR]
        k.CreateJobObjectW.restype = HANDLE
        k.SetInformationJobObject.argtypes = [HANDLE, ctypes.c_int, LPVOID, DWORD]
        k.SetInformationJobObject.restype = BOOL
        k.AssignProcessToJobObject.argtypes = [HANDLE, HANDLE]
        k.AssignProcessToJobObject.restype = BOOL
        k.InitializeProcThreadAttributeList.argtypes = [LPVOID, DWORD, DWORD, ptr(SIZE_T)]
        k.InitializeProcThreadAttributeList.restype = BOOL
        k.UpdateProcThreadAttribute.argtypes = [LPVOID, DWORD, SIZE_T, LPVOID, SIZE_T, LPVOID, LPVOID]
        k.UpdateProcThreadAttribute.restype = BOOL
        k.DeleteProcThreadAttributeList.argtypes = [LPVOID]
        k.DeleteProcThreadAttributeList.restype = None
        k.CreateProcessW.argtypes = [LPWSTR, LPWSTR, LPVOID, LPVOID, BOOL, DWORD, LPVOID, LPWSTR,
                                     ptr(STARTUPINFOEXW), ptr(PROCESS_INFORMATION)]  # fmt: skip
        k.CreateProcessW.restype = BOOL
        k.ResumeThread.argtypes = [HANDLE]
        k.ResumeThread.restype = DWORD
        k.TerminateProcess.argtypes = [HANDLE, ctypes.c_uint]
        k.TerminateProcess.restype = BOOL
        k.WaitForSingleObject.argtypes = [HANDLE, DWORD]
        k.WaitForSingleObject.restype = DWORD
        k.GetExitCodeProcess.argtypes = [HANDLE, ptr(DWORD)]
        k.GetExitCodeProcess.restype = BOOL
        k.CloseHandle.argtypes = [HANDLE]
        k.CloseHandle.restype = BOOL
        k.GetStdHandle.argtypes = [DWORD]
        k.GetStdHandle.restype = HANDLE
        k.SetHandleInformation.argtypes = [HANDLE, DWORD, DWORD]
        k.SetHandleInformation.restype = BOOL

    def fail(self, what: str) -> SandboxUnavailable:
        code = getattr(ctypes, "get_last_error")()  # noqa: B009 (absent from ctypes off Windows)
        return SandboxUnavailable(f"{what} failed (Windows error {code})")

    def container_sid(self, profile: str, create: bool) -> int:
        """The AppContainer's SID (free it with `FreeSid`). `create` makes the profile when it does not exist."""
        sid = PSID()
        if create:
            result = self.userenv.CreateAppContainerProfile(profile, profile, "Jana sandboxed worker", None, 0,
                                                            ctypes.byref(sid))  # fmt: skip
            if result == 0 and sid.value:
                return int(sid.value)
            if result & 0xFFFFFFFF != HRESULT_ALREADY_EXISTS:
                raise SandboxUnavailable(f"CreateAppContainerProfile failed (HRESULT {result & 0xFFFFFFFF:#010x})")
        result = self.userenv.DeriveAppContainerSidFromAppContainerName(profile, ctypes.byref(sid))
        if result != 0 or not sid.value:
            raise SandboxUnavailable(f"DeriveAppContainerSidFromAppContainerName failed (HRESULT {result:#x})")
        return int(sid.value)

    def grant(self, path: str, sid: int, access: int) -> None:
        """Add an inheritable allow entry for the AppContainer to `path`'s DACL (and so to everything under it)."""
        old_dacl, descriptor, new_dacl = LPVOID(), LPVOID(), LPVOID()
        a = self.advapi32
        status = a.GetNamedSecurityInfoW(path, SE_FILE_OBJECT, DACL_SECURITY_INFORMATION, None, None,
                                         ctypes.byref(old_dacl), None, ctypes.byref(descriptor))  # fmt: skip
        if status != 0:
            raise SandboxUnavailable(f"reading the access list of a worker path failed (Windows error {status})")
        try:
            if old_dacl.value and self.granted(int(old_dacl.value), sid, access):
                return  # already there: re-applying would walk the whole tree again on every start
            entry = explicit_access(sid, access)
            status = self.advapi32.SetEntriesInAclW(1, ctypes.byref(entry), old_dacl, ctypes.byref(new_dacl))
            if status != 0:
                raise SandboxUnavailable(f"building the worker's access entry failed (Windows error {status})")
            status = self.advapi32.SetNamedSecurityInfoW(path, SE_FILE_OBJECT, DACL_SECURITY_INFORMATION, None, None,
                                                         new_dacl, None)  # fmt: skip
            if status != 0:
                raise SandboxUnavailable(f"granting the worker access to its paths failed (Windows error {status})")
        finally:
            if new_dacl.value:
                self.kernel32.LocalFree(new_dacl)
            if descriptor.value:
                self.kernel32.LocalFree(descriptor)

    def granted(self, dacl: int, sid: int, access: int) -> bool:
        """Whether `dacl` already holds an inheritable allow entry giving `sid` at least `access`."""
        header = ACL.from_address(dacl)
        ace = LPVOID()
        for index in range(header.AceCount):
            if not self.advapi32.GetAce(dacl, index, ctypes.byref(ace)) or not ace.value:
                continue
            entry = ACCESS_ALLOWED_ACE.from_address(ace.value)
            if (entry.Header.AceType == ACCESS_ALLOWED_ACE_TYPE and entry.Mask & access == access
                    and entry.Header.AceFlags & INHERIT_BOTH == INHERIT_BOTH
                    and self.advapi32.EqualSid(ace.value + ACCESS_ALLOWED_ACE.SidStart.offset, sid)):  # fmt: skip
                return True
        return False

    def job(self, limits: JobLimits) -> int:
        handle = self.kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise self.fail("CreateJobObjectW")
        try:
            info = extended_limits(limits)
            if not self.kernel32.SetInformationJobObject(handle, JobObjectExtendedLimitInformation,
                                                         ctypes.byref(info), ctypes.sizeof(info)):  # fmt: skip
                raise self.fail("setting the job's limits")
            flags, rate = cpu_rate_control(limits)
            cpu = JOBOBJECT_CPU_RATE_CONTROL_INFORMATION(flags, rate)
            if not self.kernel32.SetInformationJobObject(handle, JobObjectCpuRateControlInformation,
                                                         ctypes.byref(cpu), ctypes.sizeof(cpu)):  # fmt: skip
                raise self.fail("setting the job's CPU rate cap")
            ui = JOBOBJECT_BASIC_UI_RESTRICTIONS(JOB_OBJECT_UILIMIT_ALL)
            if not self.kernel32.SetInformationJobObject(handle, JobObjectBasicUIRestrictions,
                                                         ctypes.byref(ui), ctypes.sizeof(ui)):  # fmt: skip
                raise self.fail("setting the job's UI restrictions")
        except BaseException:
            self.kernel32.CloseHandle(handle)
            raise
        return int(handle)


_GRANTED: set[tuple[str, str, int]] = set()  # (profile, path, access) granted by this process already


def prepare(policy: WindowsPolicy) -> None:
    """Make sure the AppContainer exists and may read its bundle and write its scratch (in the core, before launch).
    Read grants are applied once per process; the scratch is new on every launch."""
    api = _Api()
    sid = api.container_sid(policy.profile, create=True)
    try:
        for paths, access in ((policy.read_paths, FILE_GENERIC_READ_EXECUTE), (policy.write_paths, FILE_ALL_ACCESS)):
            for path in paths:
                key = (policy.profile, os.path.normcase(path), access)
                if key in _GRANTED:
                    continue
                if not Path(path).exists():
                    continue
                api.grant(path, sid, access)
                _GRANTED.add(key)
    finally:
        api.advapi32.FreeSid(sid)


def launch(policy: WindowsPolicy, argv: Sequence[str]) -> int:
    """In the launcher: run `argv` in the AppContainer inside the job, wait, and return its exit code."""
    api = _Api()
    k = api.kernel32
    command, extra_env = child_command(argv)
    env = {**policy.env, **extra_env}
    sid = api.container_sid(policy.profile, create=False)
    job: int | None = None
    attributes: Any = None
    process = PROCESS_INFORMATION()
    try:
        job = api.job(policy.limits)
        std = [k.GetStdHandle(DWORD(n & 0xFFFFFFFF)) for n in STD_HANDLES]
        unique = list(dict.fromkeys(std))  # stdout and stderr may share one handle; the list must not repeat it
        handles = (HANDLE * len(unique))(*unique)
        for handle in unique:
            if not handle or not k.SetHandleInformation(handle, HANDLE_FLAG_INHERIT, HANDLE_FLAG_INHERIT):
                raise api.fail("making the standard handles inheritable")
        size = SIZE_T(0)
        k.InitializeProcThreadAttributeList(None, 3, 0, ctypes.byref(size))
        attributes = ctypes.create_string_buffer(size.value)
        if not k.InitializeProcThreadAttributeList(attributes, 3, 0, ctypes.byref(size)):
            raise api.fail("InitializeProcThreadAttributeList")
        capabilities = SECURITY_CAPABILITIES(sid, None, 0, 0)  # no capabilities: no network of any kind
        caps, caps_size = ctypes.byref(capabilities), ctypes.sizeof(capabilities)
        if not k.UpdateProcThreadAttribute(attributes, 0, PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES, caps,
                                           caps_size, None, None):  # fmt: skip
            raise api.fail("setting the AppContainer")
        if not k.UpdateProcThreadAttribute(attributes, 0, PROC_THREAD_ATTRIBUTE_HANDLE_LIST, handles,
                                           ctypes.sizeof(handles), None, None):  # fmt: skip
            raise api.fail("limiting the inherited handles")
        child_policy = DWORD(PROCESS_CREATION_CHILD_PROCESS_RESTRICTED)
        # Windows 10 1607 and later; on older systems the one-process job alone refuses child processes.
        k.UpdateProcThreadAttribute(attributes, 0, PROC_THREAD_ATTRIBUTE_CHILD_PROCESS_POLICY,
                                    ctypes.byref(child_policy), ctypes.sizeof(child_policy), None, None)  # fmt: skip
        startup = STARTUPINFOEXW()
        startup.StartupInfo.cb = ctypes.sizeof(startup)
        startup.StartupInfo.dwFlags = STARTF_USESTDHANDLES
        startup.StartupInfo.hStdInput, startup.StartupInfo.hStdOutput, startup.StartupInfo.hStdError = std
        startup.lpAttributeList = ctypes.cast(attributes, LPVOID)
        block = ctypes.create_unicode_buffer(environment_block(env))
        flags = CREATE_SUSPENDED | DETACHED_PROCESS | CREATE_UNICODE_ENVIRONMENT | EXTENDED_STARTUPINFO_PRESENT
        cmdline = ctypes.create_unicode_buffer(command_line(command))
        if not k.CreateProcessW(command[0], cmdline, None, None, True, flags, block, policy.cwd,
                                ctypes.byref(startup), ctypes.byref(process)):  # fmt: skip
            raise api.fail("starting the worker in its AppContainer")
        if not k.AssignProcessToJobObject(job, process.hProcess):
            k.TerminateProcess(process.hProcess, 1)
            raise api.fail("placing the worker in its job")
        k.ResumeThread(process.hThread)
        if k.WaitForSingleObject(process.hProcess, INFINITE) == WAIT_FAILED:
            raise api.fail("waiting for the worker")
        code = DWORD(1)
        k.GetExitCodeProcess(process.hProcess, ctypes.byref(code))
        return int(code.value)
    finally:
        if attributes is not None:
            k.DeleteProcThreadAttributeList(attributes)
        for handle in (process.hThread, process.hProcess):
            if handle:
                k.CloseHandle(handle)
        if job is not None:
            k.CloseHandle(job)  # kill-on-close: nothing of the worker outlives the launcher
        api.advapi32.FreeSid(sid)


def main(args: Sequence[str] | None = None) -> int:
    """The launcher's entry. Nothing is written to stdout, which belongs to the worker's protocol."""
    try:
        policy, argv = parse_launcher_args(sys.argv[1:] if args is None else args)
        return launch(policy, argv)
    except (OSError, ValueError) as error:
        sys.stderr.write(f"worker sandbox: {error}\n")
        return 125


if __name__ == "__main__":
    raise SystemExit(main())
