"""The worker's JSON-RPC server over stdin and stdout (TOL-24, TOL-25).

Requests are handled in order on the reading thread, except `fn.call`, which runs on its own thread so the worker
can still answer `worker.ping` and `fn.cancel`. Calls cannot be pre-empted inside SymPy: cancellation is honoured
when the computation returns, and the host kills and respawns a worker whose call outlives its deadline (TOL-26).
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from importlib import metadata
from typing import Any, BinaryIO

from tool_worker import closure, plausibility
from tool_worker.budget import BudgetExceeded, call_budget, peak_rss_mb
from tool_worker.contracts import errors as contract_errors
from tool_worker.guard import PolicyDenied
from tool_worker.protocol import (
    PROTOCOL,
    WORKER_VERSION,
    Code,
    WorkerError,
    compatible,
    encode,
    error_response,
    response,
)
from tool_worker.registry import CallContext, Function

LIBRARIES = ("sympy", "mpmath", "pint", "numpy", "rdkit")


@dataclass
class Limits:
    """Per-call and protocol limits (SEC-08, TOL-24, TOL-26); the host passes the configured values at spawn."""

    cpu_ms: int = 2000
    memory_mb: int = 256
    max_expression_chars: int = 2000
    max_data_points: int = 500
    max_frame_bytes: int = 4 * 1024 * 1024
    progress_ms: int = 1000
    process_memory_mb: int = 512
    max_open_files: int = 64
    max_file_mb: int = 10
    # verification parameters (proposals of VER-03, VER-04, VER-06; set by the host from its configuration)
    max_depth: int = 64
    equivalence_points: int = 12
    equivalence_min_valid: int = 8
    numeric_digits: int = 30
    relative_tolerance: float = 1e-9
    symbolic_slice_ms: int = 800
    step_candidates: int = 64
    # mathematics checkers (M12.a): TABLE VER-09a defaults, VER-13 and VER-14 limits
    default_sf: int = 3
    min_sf: int = 2
    carried_rounding_share: float = 0.01
    money_dp: int = 2
    angle_dp: int = 1
    angle_tolerance_deg: float = 0.1
    probability_sf: int = 3
    stats_max_points: int = 500
    plot_max_points: int = 500
    plot_chord_tolerance: float = 0.001
    # accounting checkers (M12.d): places shown by default, and depreciation's rounding per year (VER-24)
    acc_dp: int = 2

    def advertised(self) -> dict[str, int]:
        return {
            "cpu_ms": self.cpu_ms,
            "memory_mb": self.memory_mb,
            "max_expression_chars": self.max_expression_chars,
            "max_data_points": self.max_data_points,
            "max_frame_bytes": self.max_frame_bytes,
        }


def _sha256(value: Any) -> str:
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _libraries() -> dict[str, str]:
    found = {}
    for name in LIBRARIES:
        if closure.hidden(name):  # installed beside a development worker, but not in its closure (PLT-03)
            continue
        try:
            found[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            continue
    return found


def _check_sizes(value: Any, limits: Limits, path: str = "args") -> None:
    """Input limits: characters per expression and points per dataset (SEC-08)."""
    if isinstance(value, str) and len(value) > limits.max_expression_chars:
        raise WorkerError(
            Code.LIMIT_EXCEEDED,
            f"{path} is longer than {limits.max_expression_chars} characters",
            limit="expression_chars",
        )
    if isinstance(value, list):
        if len(value) > limits.max_data_points:
            raise WorkerError(
                Code.LIMIT_EXCEEDED, f"{path} has more than {limits.max_data_points} items", limit="data_points"
            )
        for i, item in enumerate(value):
            _check_sizes(item, limits, f"{path}[{i}]")
    if isinstance(value, dict):
        for key, item in value.items():
            _check_sizes(item, limits, f"{path}.{key}")


@dataclass
class Server:
    functions: Mapping[str, Function]
    out: BinaryIO
    limits: Limits = field(default_factory=Limits)
    placement: str = "device"
    started: float = field(default_factory=time.monotonic)
    hello_ok: bool | None = None  # None until hello; False after a protocol mismatch
    running: dict[str, threading.Event] = field(default_factory=dict)  # call_id -> cancelled flag
    stopping: bool = False
    dropped_lines: int = 0
    threads: list[threading.Thread] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    # --- output -----------------------------------------------------------------------------------------

    def send(self, message: dict[str, Any]) -> None:
        frame = encode(message)
        if len(frame) > self.limits.max_frame_bytes and "result" in message:
            error = WorkerError(Code.LIMIT_EXCEEDED, "the result is larger than the frame limit", limit="frame_bytes")
            frame = encode(error_response(message["id"], error))
        with self._lock:
            self.out.write(frame)
            self.out.flush()

    def log(self, level: str, message: str) -> None:
        self.send({"jsonrpc": "2.0", "method": "worker.log", "params": {"level": level, "message": message[:2000]}})

    # --- input ------------------------------------------------------------------------------------------

    def handle_line(self, line: bytes) -> None:
        try:
            request = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError):
            request = None
        if not isinstance(request, dict):
            # Lines that are not JSON objects are dropped and counted (TOL-24).
            self.dropped_lines += 1
            self.log("warn", f"dropped a line that is not a JSON object ({self.dropped_lines} so far)")
            return
        request_id = request.get("id")
        try:
            problems = contract_errors("tools/worker-protocol#/$defs/Request", request)
            if problems:
                code = Code.METHOD_NOT_FOUND if not self._known(request.get("method")) else Code.INVALID_PARAMS
                raise WorkerError(code, "the request does not match the worker protocol", reason=problems[0][:200])
            self.dispatch(request)
        except WorkerError as error:
            self.send(error_response(request_id if isinstance(request_id, str) else None, error))

    @staticmethod
    def _known(method: Any) -> bool:
        return method in {
            "worker.hello",
            "worker.ping",
            "fn.call",
            "fn.cancel",
            "worker.shutdown",
            "exec.run",
            "exec.interrupt",
            "exec.reset",
        }

    def dispatch(self, request: dict[str, Any]) -> None:
        method, params, request_id = request["method"], request["params"], request["id"]
        if method == "worker.hello":
            self.hello(request_id, params)
        elif method == "worker.ping":
            self.send(response(request_id, self.pong()))
        elif method.startswith("exec."):
            # No exec path exists in a device worker (SEC-09, TOL-25).
            raise WorkerError(Code.EXEC_DISABLED, "code execution is not available in this worker")
        elif self.hello_ok is None:
            raise WorkerError(Code.INVALID_REQUEST, "worker.hello must come first")
        elif not self.hello_ok and method == "fn.call":
            raise WorkerError(Code.VERSION_MISMATCH, "protocol mismatch", expected_protocol=PROTOCOL)
        elif method == "fn.call":
            self.start_call(request_id, params)
        elif method == "fn.cancel":
            flag = self.running.get(params["call_id"])
            if flag:
                flag.set()
            self.send(response(request_id, {"kind": "ack", "ok": flag is not None}))
        elif method == "worker.shutdown":
            self.stopping = True
            self.send(response(request_id, {"kind": "ack", "ok": True}))

    def hello(self, request_id: str, params: dict[str, Any]) -> None:
        if not compatible(params["protocol"], PROTOCOL):
            self.hello_ok = False
            raise WorkerError(Code.VERSION_MISMATCH, "protocol mismatch", expected_protocol=PROTOCOL)
        if params["placement"] != self.placement:
            self.hello_ok = False
            raise WorkerError(Code.INVALID_PARAMS, f"this is a {self.placement} worker")
        self.hello_ok = True
        functions = [
            {"name": f.name, "version": f.version, "schema_sha256": f.schema_sha256}
            for f in sorted(self.functions.values(), key=lambda f: f.name)
        ]
        self.send(
            response(
                request_id,
                {
                    "kind": "hello",
                    "protocol": PROTOCOL,
                    "worker_version": WORKER_VERSION,
                    "placement": self.placement,
                    "functions": functions,
                    "libraries": _libraries(),
                    "limits": self.limits.advertised(),
                    "exec_available": False,
                },
            )
        )

    def pong(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "kind": "pong",
            "uptime_ms": int((time.monotonic() - self.started) * 1000),
            "busy": bool(self.running),
        }
        rss_mb = peak_rss_mb()
        if rss_mb is not None:  # optional in the protocol: absent where the platform cannot say
            out["rss_mb"] = round(rss_mb, 1)
        return out

    # --- calls ------------------------------------------------------------------------------------------

    def start_call(self, request_id: str, params: dict[str, Any]) -> None:
        function = self.functions.get(params["name"])
        if function is None or function.version != params["version"]:
            raise WorkerError(Code.UNKNOWN_FUNCTION, f"no function {params['name']} version {params['version']}")
        problems = sorted(function.validator().iter_errors(params["args"]), key=lambda e: list(e.path))
        if problems:
            first = problems[0]
            where = "/".join(str(p) for p in first.path) or "args"
            raise WorkerError(Code.INVALID_PARAMS, f"{where}: {first.message}"[:2000], reason=where)
        _check_sizes(params["args"], self.limits)
        cancelled = threading.Event()
        self.running[params["call_id"]] = cancelled
        thread = threading.Thread(target=self._run, args=(request_id, params, function, cancelled), daemon=True)
        self.threads = [t for t in self.threads if t.is_alive()] + [thread]
        thread.start()
        threading.Thread(target=self._progress, args=(params["call_id"], thread), daemon=True).start()

    def _progress(self, call_id: str, thread: threading.Thread) -> None:
        """Heartbeats for a running call (TOL-11): one `fn.progress` per interval until it finishes."""
        began = time.monotonic()
        while True:
            thread.join(self.limits.progress_ms / 1000)
            if not thread.is_alive() or call_id not in self.running:
                return
            elapsed = int((time.monotonic() - began) * 1000)
            progress = {"call_id": call_id, "elapsed_ms": elapsed}
            self.send({"jsonrpc": "2.0", "method": "fn.progress", "params": progress})

    def _run(self, request_id: str, params: dict[str, Any], function: Function, cancelled: threading.Event) -> None:
        context = CallContext(
            call_id=params["call_id"],
            check_id=params["check_id"],
            conventions=params.get("conventions", {}),
            limits=self.limits.advertised(),
            cancelled=cancelled.is_set,
            seed=int(_sha256(params["args"])[:16], 16),  # VER-01: sampling seeded from the arguments' hash
            settings=asdict(self.limits),
        )
        try:
            with call_budget(self.limits.cpu_ms, self.limits.memory_mb):  # SEC-08: CPU and memory per call
                value, extras = function.implementation(params["args"], context)
            if cancelled.is_set():
                raise WorkerError(Code.CANCELLED, "the call was cancelled")
            implausible = plausibility.flags(function.name, value, context.conventions)  # VER-26: flag, never change
            if implausible:
                extras = {**extras, "warnings": [*extras.get("warnings", []), *implausible]}
            result: dict[str, Any] = {
                "kind": "fn",
                "value": value,
                "check": self._check(function, params, value, extras),
            }
            for key in ("latex", "working", "warnings"):
                if extras.get(key):
                    result[key] = extras[key]
            problems = contract_errors("tools/worker-protocol#/$defs/FnCallResult", result)
            if problems:
                raise WorkerError(Code.TOOL_RAISED, "the function produced an invalid result", reason=problems[0][:200])
            self.send(response(request_id, result))
        except WorkerError as error:
            self.send(error_response(request_id, error))
        except (PolicyDenied, PermissionError) as denied:
            # A refused import, a disabled capability or an OS sandbox refusal (SEC-06, SEC-07).
            refused = WorkerError(
                Code.POLICY_DENIED,
                "the sandbox refused the operation",
                reason=str(denied)[:200] or type(denied).__name__,
                exception_type=type(denied).__name__,
            )
            self.send(error_response(request_id, refused))
        except BudgetExceeded as over:
            what = "CPU time" if over.limit == "cpu" else "memory"
            self.send(error_response(request_id, WorkerError(Code.LIMIT_EXCEEDED, f"{what} limit", limit=over.limit)))
        except MemoryError:
            self.send(error_response(request_id, WorkerError(Code.LIMIT_EXCEEDED, "memory limit", limit="memory")))
        except RecursionError:
            deep = WorkerError(Code.LIMIT_EXCEEDED, "the expression is nested too deeply", limit="components")
            self.send(error_response(request_id, deep))
        except Exception as exc:  # a function bug: report the type only, never a traceback or arguments
            raised = WorkerError(Code.TOOL_RAISED, "the function failed", exception_type=type(exc).__name__)
            self.send(error_response(request_id, raised))
        finally:
            self.running.pop(params["call_id"], None)

    def _check(self, function: Function, params: dict[str, Any], value: Any, extras: dict[str, Any]) -> dict[str, Any]:
        record: dict[str, Any] = {
            "check_id": params["check_id"],
            "function": function.name,
            "function_version": function.version,
            "worker_version": WORKER_VERSION,
            "args_sha256": _sha256(params["args"]),
            "result_sha256": _sha256(value),
            "method": extras.get("method", "symbolic"),
            "confidence": extras.get("confidence", "proved"),
        }
        for key in ("seed", "constants", "inputs_from"):
            if key in extras:
                record[key] = extras[key]
        return record

    # --- loop -------------------------------------------------------------------------------------------

    def serve(self, stdin: BinaryIO, grace_s: float = 5.0) -> None:
        for line in stdin:
            if line.strip():
                self.handle_line(line)
            if self.stopping:
                break
        # End of input or shutdown: let calls in flight answer before exiting.
        for thread in self.threads:
            thread.join(grace_s)
