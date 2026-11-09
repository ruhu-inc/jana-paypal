"""The tool round (AGT-07, TOL-08, TOL-09, TOL-15, AGT-11 tool guards, AGT-16 receipts).

Each model tool call is repaired (TOL-08) or answered through the hidden `invalid` tool; a call to a registered
tool the role may not call is passed to the executor so it returns `policy_denied` or `unavailable` with no action
(SEC-04, TOL-06). Valid calls run through the executor, which plans parallel-safe calls together and barriers in
order (TOL-11). Every call gets one result, so the transcript stays well formed, and exactly one `running` and one
terminal `tool.state` event."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any

from agent_core.config import Settings
from agent_core.engine.events import EventStream
from agent_core.engine.memory_tools import items_read
from agent_core.engine.receipts import Phase, Receipts, default_receipts
from agent_core.engine.state import SessionState, exponent_ok, expression_key
from agent_core.router import ToolCall
from agent_core.tools.dedupe import NON_RETRYABLE, call_key
from agent_core.tools.executor import ToolCall as ExecCall
from agent_core.tools.executor import ToolExecutor, plan
from agent_core.tools.policy import delegation_call, granted
from agent_core.tools.repair import UNPARSED, RawCall, Strikes, repair_call
from agent_core.tools.results import error_result
from agent_core.tools.spec import SessionCtx
from agent_core.trace.record import TraceBuilder, repair_classes, sha256_of

RENDER_CLASS = "ephemeral"


def _checked_expressions(name: str, args: dict[str, Any], data: Any) -> list[str]:
    """Keep only whole, proved results; sampling, conditional domains and parsing are not proof."""
    if not isinstance(data, dict) or data.get("check", {}).get("confidence") != "proved":
        return []
    value = data.get("value")
    if not isinstance(value, dict) or args.get("assumptions"):
        return []
    found: list[Any] = []
    if name == "math_check_equivalence" and value.get("verdict") == "equivalent":
        found = [args.get("a"), args.get("b")]
    elif (
        name == "math_simplify"
        and value.get("status") == "ok"
        and args.get("form", "simplest")
        in {
            "simplest",
            "expanded",
            "factored",
        }
    ):
        result = value.get("result")
        if isinstance(result, dict):
            found = [result.get("text"), data.get("latex")]
    return list(dict.fromkeys(expression_key(s) for s in found if isinstance(s, str) and 0 < len(s) <= 4096))


def _checked_numbers(value: Any) -> list[Fraction]:
    """Complete scalar values, never identifier digits or coefficients inside symbolic expressions."""
    if isinstance(value, dict):
        # Exact-number objects may also contain numerator/denominator or rendering metadata. Those are not
        # separate answers: use their complete scalar representations only.
        parts = (
            [value[k] for k in ("text", "decimal") if k in value]
            if ("text" in value or "decimal" in value)
            else (v for k, v in value.items() if k != "unit")
        )
        return [number for part in parts for number in _checked_numbers(part)]
    if isinstance(value, list):
        return [number for part in value for number in _checked_numbers(part)]
    if value is None or isinstance(value, bool):
        return []
    text = str(value).strip().replace("\u2212", "-")
    if len(text) > 4096 or not exponent_ok(text):
        return []
    try:
        return [Fraction(text)]
    except (ValueError, ZeroDivisionError):
        return []


@dataclass
class Outcome:
    call_id: str
    name: str
    result: dict[str, Any]


@dataclass
class ToolRound:
    """Runs one round of model tool calls for a role and keeps the turn's strike and guard memory."""

    executor: ToolExecutor
    settings: Settings
    events: EventStream | None
    trace: TraceBuilder
    state: SessionState
    turn_number: int
    strikes: Strikes = field(default_factory=Strikes)
    failing_calls: dict[str, int] = field(default_factory=dict)  # identical failing call -> count (AGT-11)
    started: dict[str, str] = field(default_factory=dict)  # call_id -> receipt state
    history: list[str] = field(default_factory=list)  # call signatures in order, for the cycle guard
    calls_made: int = 0
    part_ids: dict[str, str] = field(default_factory=dict)  # call_id -> tool part
    log: list[dict[str, Any]] = field(default_factory=list)  # role, name, status, args per call (evaluation evidence)
    # TOL-13: calls that failed with a non-retryable error this turn, by call key, with the first error's message.
    terminal: dict[str, str] = field(default_factory=dict)
    receipt_texts: Receipts = field(default_factory=default_receipts)  # TOL-14 wording, set by the engine

    async def run(
        self,
        calls: Sequence[ToolCall],
        role: str,
        session: SessionCtx,
        visible: Sequence[str],
        truncated: bool,
        cancelled: asyncio.Event | None = None,
    ) -> list[Outcome]:
        outcomes: list[Outcome | None] = [None] * len(calls)
        runnable: list[tuple[int, ExecCall]] = []
        schemas = {
            name: self.executor.registry.specs[name].params for name in visible if name in self.executor.registry.specs
        }
        for index, call in enumerate(calls):
            call_id = f"call_{self.turn_number}_{self.calls_made + index}"
            raw = RawCall(call.id, call.name, dict(call.arguments), truncated=truncated and UNPARSED in call.arguments)
            signature = raw.name + json.dumps(raw.arguments, sort_keys=True, default=str)
            self.history.append(signature)
            if self.strikes.blocked(raw):
                outcomes[index] = self._error(
                    call_id,
                    raw.name,
                    "repeated_failure_blocked",
                    "the same malformed call was already refused twice",
                    [],
                )
                continue
            if self.failing_calls.get(signature, 0) >= self.settings.guard_same_call_block:
                outcomes[index] = self._error(
                    call_id, raw.name, "repeated_failure_blocked", "this call kept failing with the same arguments", []
                )
                self.trace.guard("same_failing_call", "blocked")
                continue
            if delegation_call(raw.name):
                # No model delegates: only the engine calls a role (AGT-03, AGT-14). Refused before anything runs.
                outcomes[index] = self._error(call_id, raw.name, "policy_denied", "only the engine calls another role",
                                              [])  # fmt: skip
                continue
            blocked = self.terminal.get(self._key(raw.name, raw.arguments))
            if blocked is not None:
                outcomes[index] = self._error(call_id, raw.name, "repeated_failure_blocked",
                                              f"this exact call already failed this turn ({blocked[:300]}); change the "
                                              "arguments", [])  # fmt: skip
                continue
            if raw.name in self.executor.registry.specs and raw.name not in visible and UNPARSED not in raw.arguments:
                spec = self.executor.registry.specs[raw.name]
                if granted(role, spec, self.executor.grants) and spec.meta.risk != "ui":
                    # Granted but not offered on this call (node filter, round cap, hidden): unavailable (TOL-06).
                    outcomes[index] = self._error(
                        call_id, raw.name, "unavailable", "this tool is not offered right now", [], resolved=raw.name
                    )
                    continue
                runnable.append((index, ExecCall(call_id, raw.name, raw.arguments)))  # policy decides (SEC-04)
                self._record(call_id, raw.name, raw.name, [], raw.arguments)
                continue
            repaired = repair_call(raw, call_id, list(visible), schemas, self.settings)
            if repaired.error:
                self.strikes.record_malformed(raw)
                outcomes[index] = self._error(
                    call_id, raw.name, repaired.error, repaired.detail, repaired.repairs, resolved=repaired.name
                )
                continue
            assert repaired.name is not None and repaired.arguments is not None
            self._record(call_id, raw.name, repaired.name, repaired.repairs, repaired.arguments)
            runnable.append((index, ExecCall(call_id, repaired.name, repaired.arguments)))
        self.calls_made += len(calls)
        if runnable:
            for _, pending in runnable:
                self._state(pending.call_id, pending.name, {"status": "pending"})
                self._state(pending.call_id, pending.name, {"status": "running", "title": self._title(pending.name)})
                self.started[pending.call_id] = "not_started"
            results = await self._run_batch([c for _, c in runnable], role, session, cancelled)
            for index, pending in runnable:
                result = results.get(pending.call_id)
                if result is None:  # stopped before it ran, or while it ran (TOL-14)
                    phase: Phase = "running" if self.started[pending.call_id] == "abandoned" else "not_started"
                    result = self._cancelled(pending, phase)
                outcomes[index] = self._finish(pending, result)
        done = [o for o in outcomes if o is not None]
        by_id = {c.call_id: c for _, c in runnable}
        for outcome in done:
            called = by_id.get(outcome.call_id)
            self.log.append(
                {
                    "role": role,
                    "name": outcome.name,
                    "status": outcome.result["status"],
                    "args": dict(called.args) if called else {},
                }
            )
        return done

    async def _run_batch(self, calls: list[ExecCall], role: str, session: SessionCtx,
                         cancelled: asyncio.Event | None) -> dict[str, dict[str, Any]]:  # fmt: skip
        """The executor's plan (TOL-11), group by group, watching for a stop: a group that has not started never
        runs; a group running when the stop comes is abandoned, except calls that had already finished."""
        results: dict[str, dict[str, Any]] = {}
        pool = asyncio.Semaphore(self.settings.worker_pool_size)

        async def one(call: ExecCall) -> dict[str, Any]:
            async with pool:
                return await self.executor.run_one(call, role, session)

        def meta_of(name: str) -> Any:
            spec = self.executor.registry.specs.get(name)
            return spec.meta if spec else None

        for group in plan(calls, meta_of):
            if cancelled is not None and cancelled.is_set():
                break
            tasks = {calls[i].call_id: asyncio.ensure_future(one(calls[i])) for i in group}
            stop: asyncio.Future[Any] | None = None
            try:
                if cancelled is None:
                    await asyncio.gather(*tasks.values())
                else:
                    stop = asyncio.ensure_future(cancelled.wait())
                    pending: set[asyncio.Future[Any]] = set(tasks.values())
                    while pending and not stop.done():
                        _, pending = await asyncio.wait({stop, *pending}, return_when=asyncio.FIRST_COMPLETED)
                        pending.discard(stop)
                    stop.cancel()
                    if not all(t.done() for t in tasks.values()):
                        for call_id, task in tasks.items():
                            if task.done() and not task.cancelled() and task.exception() is None:
                                results[call_id] = task.result()
                                self.started[call_id] = "executed"
                            else:
                                task.cancel()
                                self.started[call_id] = "abandoned"
                        await asyncio.gather(*tasks.values(), return_exceptions=True)  # drained: nothing left running
                        break
            except asyncio.CancelledError:
                if stop is not None:
                    stop.cancel()
                for call_id, task in tasks.items():
                    if not task.done():
                        task.cancel()
                        self.started[call_id] = "abandoned"
                await asyncio.gather(*tasks.values(), return_exceptions=True)
                raise
            for call_id, task in tasks.items():
                results[call_id] = task.result()
                self.started[call_id] = "executed"
        return results

    def _cancelled(self, call: ExecCall, phase: Phase) -> dict[str, Any]:
        """A stopped call's result: `cancelled`, its output the receipt for its tool class and phase (TOL-14)."""
        spec = self.executor.registry.specs.get(call.name)
        text = self.receipt_texts.text(call.name, spec.meta.executor if spec else None, phase)
        result = error_result("cancelled", "the turn was stopped", self._meta(call), action_taken=False)
        result["output"] = text
        return result

    def _key(self, name: str, arguments: dict[str, Any]) -> str:
        spec = self.executor.registry.specs.get(name)
        return call_key(name, spec.version if spec else 1, arguments)

    # --- helpers ------------------------------------------------------------------------------------------

    def _meta(self, call: ExecCall) -> dict[str, Any]:
        spec = self.executor.registry.specs.get(call.name)
        return {
            "call_id": call.call_id,
            "tool": call.name,
            "tool_version": spec.version if spec else 1,
            "duration_ms": 0,
        }

    def _title(self, name: str) -> dict[str, str]:
        spec = self.executor.registry.specs.get(name)
        label = spec.meta.activity_label if spec and spec.meta.activity_label else "Used a tool"
        return {"pre": label[:80]}

    def _state(self, call_id: str, name: str, state: dict[str, Any]) -> None:
        if self.events is not None:
            if call_id not in self.part_ids:
                self.part_ids[call_id] = self.events.start_part("tool")
            part = self.part_ids[call_id]
            self.events.emit(
                {
                    "type": "tool.state",
                    "part_id": part,
                    "call_id": call_id,
                    "name": name,
                    "render_class": RENDER_CLASS,
                    "state": state,
                }
            )
            if state["status"] in ("completed", "error", "cancelled"):
                self.events.end_part(part, "complete" if state["status"] == "completed" else "error")

    def _record(self, call_id: str, emitted: str, name: str, repairs: list[str], arguments: dict[str, Any]) -> None:
        spec = self.executor.registry.specs.get(name)
        self.trace.tool_call(
            {
                "call_id": call_id,
                "request_id": _ulid_from(call_id),
                "name_emitted": emitted[:128],
                "name": name,
                "toolset": spec.toolset if spec else "meta",
                "executor": spec.meta.executor if spec else "host",
                "status": "ok",
                "duration_ms": 0,
                "repairs": repair_classes(repairs),
                "args_sha256": sha256_of(arguments),
            }
        )

    def _error(
        self, call_id: str, name: str, code: str, detail: str, repairs: list[str], resolved: str | None = None
    ) -> Outcome:
        """The hidden `invalid` tool's result: original name, code, no action taken, how to retry (TOL-09)."""
        meta = {"call_id": call_id, "tool": "invalid", "tool_version": 1, "duration_ms": 0}
        result = error_result(code, f"{name[:60]}: {detail}", meta, max_chars=self.settings.tool_error_max_chars)
        result["meta"]["tool"] = "invalid"
        self.trace.tool_call(
            {
                "call_id": call_id,
                "request_id": _ulid_from(call_id),
                "name_emitted": name[:128],
                "name": resolved or "invalid",
                "toolset": "meta",
                "executor": "host",
                "status": "error",
                "error_code": code,
                "duration_ms": 0,
                "repairs": repair_classes(repairs),
                "args_sha256": sha256_of(detail),
            }
        )
        self._state(call_id, "invalid", {"status": "pending"})
        self._state(call_id, "invalid", {"status": "running", "title": {"pre": "Could not run a tool call"}})
        self._state(
            call_id,
            "invalid",
            {
                "status": "error",
                "title": {"pre": "Could not run a tool call"},
                "code": code,
                "message": result["error"]["message"][:2000],
                "retryable": bool(result["error"].get("retryable", False)),
            },
        )
        return Outcome(call_id, "invalid", result)

    def _finish(self, call: ExecCall, result: dict[str, Any]) -> Outcome:
        status = result["status"]
        for entry in reversed(self.trace.tool_calls):
            if entry["call_id"] == call.call_id:
                entry["status"] = {"ok": "ok", "error": "error", "partial": "partial", "cancelled": "cancelled"}[status]
                entry["duration_ms"] = int(result["meta"].get("duration_ms", 0))
                entry["result_sha256"] = sha256_of(result.get("output", ""))
                if status == "error":
                    entry["error_code"] = result["error"]["code"]
                if result["meta"].get("truncated"):
                    entry["truncated"] = True
                checks = result["meta"].get("checks") or []
                if checks:
                    entry["check_id"] = checks[0]["check_id"]
                break
        checks = result["meta"].get("checks") or []
        if status == "ok" and checks:
            check_id = checks[0]["check_id"]
            data = result.get("data")
            warnings = data.get("warnings") if isinstance(data, dict) else None
            flags = [str(w.get("message", ""))[:300] for w in warnings or [] if isinstance(w, dict)
                     and w.get("code") == "implausible"]  # fmt: skip
            # Only returned values are numerical proof. The presentation wrapper includes check ids and may
            # include warning bounds; digits in either are not values the worker calculated. Parsing syntax
            # also attests structure, not a derived answer.
            produced = data.get("value") if isinstance(data, dict) else None
            if call.name == "math_parse":
                produced = None
            self.state.check_values[check_id] = _checked_numbers(produced)
            self.state.check_expressions[check_id] = _checked_expressions(call.name, dict(call.args), data)
            if flags:
                self.state.implausible[check_id] = flags  # VER-26: a probable input error until the Checker reviews it
        signature = call.name + json.dumps(call.args, sort_keys=True, default=str)
        if status == "error" and result["error"]["code"] in NON_RETRYABLE:
            # TOL-13: an identical call later in this turn is answered without running, quoting this error.
            self.terminal.setdefault(self._key(call.name, dict(call.args)), result["error"]["message"])
        if status == "error" and result["error"].get("retryable"):
            self.failing_calls[signature] = self.failing_calls.get(signature, 0) + 1
            if self.failing_calls[signature] == self.settings.guard_same_call_warn:
                self.trace.guard("same_failing_call", "warned")
        title = self._title(call.name)
        if status == "ok":
            terminal: dict[str, Any] = {"status": "completed", "title": title}
            if checks:
                terminal["check_id"] = checks[0]["check_id"]
            read = items_read(call.name, result.get("data"))
            if read is not None:
                # The memory-use line (MEM-28) is made from this count, taken from the handler's own result: the
                # model's words can neither add the line nor remove it.
                terminal["output"] = {"items": read}
        elif status == "cancelled":
            terminal = {"status": "cancelled", "title": title, "receipt": result["output"][:400]}
        else:
            terminal = {
                "status": "error",
                "title": title,
                "code": result["error"]["code"],
                "message": result["error"]["message"][:2000],
                "retryable": bool(result["error"]["retryable"]),
            }
        self._state(call.call_id, call.name, terminal)
        return Outcome(call.call_id, call.name, result)

    def cycle_detected(self) -> bool:
        """An identical call sequence of period ≤ N repeating M times (AGT-11 call cycle)."""
        history = self.history
        for period in range(1, self.settings.guard_cycle_period + 1):
            span = period * self.settings.guard_cycle_repeats
            if len(history) >= span:
                tail = history[-span:]
                if all(tail[i] == tail[i % period] for i in range(span)):
                    return True
        return False

    def receipts(self) -> dict[str, str]:
        return dict(self.started)


def _ulid_from(call_id: str) -> str:
    import hashlib

    from agent_core.trace.ids import CROCKFORD

    digest = int(hashlib.sha256(call_id.encode()).hexdigest(), 16)
    return "".join(CROCKFORD[(digest >> (5 * i)) & 31] for i in range(26))


def tool_message(outcome: Outcome) -> dict[str, Any]:
    """The tool-role message for the next request (bounded output; TOL-12)."""
    return {"call_id": outcome.call_id, "name": outcome.name, "content": outcome.result["output"]}


def now_ms() -> int:
    return int(time.time() * 1000)
