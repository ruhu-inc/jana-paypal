"""Worker side of the JSON-RPC protocol (TOL-24, TOL-25): framing, versions and error codes.

Frames are compact JSON objects, one per line, ASCII-escaped so no frame contains a raw line break."""

from __future__ import annotations

import json
from enum import IntEnum
from typing import Any

# The worker protocol version (ARC-29): equal majors are required, and a worker serves any host whose minor is at
# most its own (minors are additive).
PROTOCOL = {"major": 1, "minor": 0}
WORKER_VERSION = "0.1.0"


class Code(IntEnum):
    PARSE_ERROR = -32700
    INVALID_REQUEST = -32600
    METHOD_NOT_FOUND = -32601
    INVALID_PARAMS = -32602
    INTERNAL_ERROR = -32603
    VERSION_MISMATCH = 1
    UNKNOWN_FUNCTION = 2
    PARSE_REJECTED = 3
    DOMAIN_ERROR = 4
    LIMIT_EXCEEDED = 5
    CANCELLED = 6
    BUSY = 7
    EXEC_DISABLED = 8
    CELL_STATE_LOST = 9
    TOOL_RAISED = 10
    POLICY_DENIED = 12


class WorkerError(Exception):
    """An error answered to the host as a JSON-RPC error object."""

    def __init__(self, code: Code, message: str, **data: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message[:2000]
        self.data = {k: v for k, v in data.items() if v is not None}

    def as_error(self) -> dict[str, Any]:
        error: dict[str, Any] = {"code": int(self.code), "message": self.message}
        if self.data:
            error["data"] = self.data
        return error


def compatible(host: dict[str, int], worker: dict[str, int]) -> bool:
    return host["major"] == worker["major"] and worker["minor"] >= host["minor"]


def encode(message: dict[str, Any]) -> bytes:
    """One frame: compact, ASCII-escaped JSON and a single newline."""
    return json.dumps(message, ensure_ascii=True, separators=(",", ":"), allow_nan=False).encode("ascii") + b"\n"


def response(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def error_response(request_id: Any, error: WorkerError) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": error.as_error()}
