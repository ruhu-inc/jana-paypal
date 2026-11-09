"""Worker error codes and their tool error codes (TOL-25 mapping onto TABLE TOL-10a)."""

from __future__ import annotations

# Application codes; 11 WORKER_RESTARTED is host-side only and never sent by a worker.
WORKER_RESTARTED = 11
BUSY = 7

APPLICATION_CODES: dict[int, str] = {
    1: "worker_unavailable",  # VERSION_MISMATCH
    2: "unavailable",  # UNKNOWN_FUNCTION
    3: "invalid_args",  # PARSE_REJECTED (with position)
    4: "domain_error",  # DOMAIN_ERROR
    5: "limit_exceeded",  # LIMIT_EXCEEDED
    6: "cancelled",  # CANCELLED
    7: "execution_error",  # BUSY: retried by the host once; a second BUSY is an execution error
    8: "unavailable",  # EXEC_DISABLED
    9: "execution_error",  # CELL_STATE_LOST
    10: "execution_error",  # TOOL_RAISED
    11: "worker_crashed",  # WORKER_RESTARTED
    12: "policy_denied",  # POLICY_DENIED: the SEC-07 allow-list or audit hook refused an operation
}
# Standard JSON-RPC codes.
STANDARD_CODES: dict[int, str] = {
    -32700: "execution_error",
    -32600: "execution_error",
    -32601: "unavailable",
    -32602: "invalid_args",
    -32603: "execution_error",
}


def tool_error_code(worker_code: int) -> str:
    return APPLICATION_CODES.get(worker_code) or STANDARD_CODES.get(worker_code) or "execution_error"
