"""Running tool calls for the engine (TOL-02 to TOL-04, TOL-10 to TOL-12, TOL-24).

Each call returns one envelope valid against the tool-result contract, whatever happens: unknown, hidden, denied,
invalid, timed out, crashed or successful. Batches are planned from metadata only (TOL-03, TOL-11): adjacent
parallel-safe calls without side effects run together up to the pool size; any call with side effects or an
interactive call is a barrier; results return in the model's order."""

from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from typing import Any

from jsonschema import Draft202012Validator

from agent_core.config import Settings
from agent_core.tools.dedupe import DedupeCache, cacheable, call_key, served_from_cache
from agent_core.tools.generated.register import REGISTER
from agent_core.tools.policy import denial
from agent_core.tools.registry import Registry
from agent_core.tools.results import envelope_errors, error_result, ok_result
from agent_core.tools.spec import CallContext, InvalidArguments, SessionCtx, ToolSpec
from agent_core.tools.spill import SpillStore, bound
from agent_core.tools.worker import WorkerSupervisor

CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
STALL_INTERVALS = 3  # missed heartbeats that count as a stall (TOL-11)


def new_ulid(now_ms: int | None = None) -> str:
    stamp = now_ms if now_ms is not None else int(time.time() * 1000)
    value = (stamp << 80) | secrets.randbits(80)
    return "".join(CROCKFORD[(value >> shift) & 31] for shift in range(125, -1, -5))


@dataclass(frozen=True)
class ToolCall:
    call_id: str
    name: str
    args: dict[str, Any]


def plan(calls: list[ToolCall], meta_of: Callable[[str], Any]) -> list[list[int]]:
    """Groups of call indexes in run order (TOL-11). Unknown tools run alone so their error is quick."""
    groups: list[list[int]] = []
    current: list[int] = []
    for i, call in enumerate(calls):
        meta = meta_of(call.name)
        barrier = meta is None or meta.side_effects or meta.interactive or not meta.parallel_safe
        if barrier:
            if current:
                groups.append(current)
                current = []
            groups.append([i])
        else:
            current.append(i)
    if current:
        groups.append(current)
    return groups


@dataclass
class ToolExecutor:
    registry: Registry
    settings: Settings
    spill: SpillStore
    supervisor: WorkerSupervisor | None = None
    grants: dict[str, list[str]] = field(default_factory=lambda: REGISTER["roleGrants"])
    host_executors: dict[str, Callable[[dict[str, Any], CallContext], Coroutine[Any, Any, Any]]] = field(
        default_factory=dict
    )
    safety_blocked: Callable[[str], bool] | None = None
    dedupe: DedupeCache = field(init=False)
    _inflight: dict[tuple[str, str], asyncio.Future[dict[str, Any]]] = field(init=False, default_factory=dict)

    def __post_init__(self) -> None:
        self.dedupe = DedupeCache(self.settings.tool_dedupe_entries)

    # --- visibility (TOL-06) ----------------------------------------------------------------------------

    def available(self, spec: ToolSpec, session: SessionCtx) -> bool:
        if self.safety_blocked is not None and self.safety_blocked(spec.name):
            return False
        if spec.meta.executor == "worker":
            if self.supervisor is None or not self.supervisor.compute_available():
                return False
            advertised = self.supervisor.functions.get(spec.name)
            if not advertised:
                return False
            return bool(advertised["version"] == spec.version
                        and advertised["schema_sha256"] == _schema_sha256(spec.params))  # fmt: skip
        if spec.meta.executor == "host":
            return spec.name in self.host_executors
        return False  # client tools are applied by the engine from the tutor turn (M5)

    # --- one call -----------------------------------------------------------------------------------------

    def _meta(self, call: ToolCall, spec: ToolSpec | None, started: float) -> dict[str, Any]:
        meta: dict[str, Any] = {
            "call_id": call.call_id,
            "tool": call.name if spec is None else spec.name,
            "tool_version": spec.version if spec else 1,
            "duration_ms": int((time.monotonic() - started) * 1000),
        }
        if spec and spec.meta.dedupe:
            meta["dedupe_key"] = call_key(spec.name, spec.version, call.args)
        return meta

    async def run_one(self, call: ToolCall, role: str, session: SessionCtx) -> dict[str, Any]:
        started = time.monotonic()
        spec = self.registry.specs.get(call.name)
        cap = self.settings.tool_error_max_chars
        if spec is None:
            visible = ", ".join(sorted(n for n, s in self.registry.specs.items() if self.available(s, session)))
            detail = f"there is no tool named {call.name[:80]}; the tools are: {visible or 'none'}"
            result = error_result("unknown_tool", detail, self._meta(call, None, started), max_chars=cap)
            return self._checked(result)
        if self.safety_blocked is not None and self.safety_blocked(spec.name):
            return self._checked(
                error_result(
                    "policy_denied", "OPS-11: tool disabled for safety", self._meta(call, spec, started), max_chars=cap
                )
            )
        refused = denial(role, spec, session, self.grants)
        if refused:
            return self._checked(error_result("policy_denied", refused, self._meta(call, spec, started), max_chars=cap))
        if not cacheable(spec):
            return await self._run_one(call, spec, session, started)
        # Filed under everything the worker is given besides the arguments: the pack's conventions and the session's
        # plausibility bands, since a result depends on both (TOL-13, VER-26, CUR-28).
        conventions = (
            {"pack": session.conventions, "bands": band_conventions(session.plausibility)}
            if spec.meta.executor == "worker"
            else None
        )
        key = call_key(spec.name, spec.version, call.args, conventions)
        stored = self.dedupe.get(session.session_id, key)
        if stored is not None:  # a repeat in this session: the stored result, no worker call (TOL-13)
            return self._checked(served_from_cache(stored, call.call_id))
        running = self._inflight.get((session.session_id, key))
        if running is not None:  # the same call in the same batch: wait for the first and share its result
            first = await asyncio.shield(running)
            if first["status"] == "ok":
                return self._checked(served_from_cache(first, call.call_id))
            return await self._run_one(call, spec, session, started)
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._inflight[(session.session_id, key)] = future
        try:
            result = await self._run_one(call, spec, session, started)
        except asyncio.CancelledError:
            future.cancel()  # abandoned (TOL-14): a waiting twin is abandoned with it
            raise
        except Exception as error:
            future.set_exception(error)
            future.exception()  # retrieved: a waiting twin re-raises it, nothing is left unobserved
            raise
        finally:
            self._inflight.pop((session.session_id, key), None)
        future.set_result(result)
        if result["status"] == "ok":
            self.dedupe.put(session.session_id, key, result)
        return result

    async def _run_one(self, call: ToolCall, spec: ToolSpec, session: SessionCtx, started: float) -> dict[str, Any]:
        cap = self.settings.tool_error_max_chars
        if not self.available(spec, session):
            detail = f"{spec.name} is not available right now"
            return self._checked(error_result("unavailable", detail, self._meta(call, spec, started), max_chars=cap))
        problems = sorted(Draft202012Validator(spec.params).iter_errors(call.args), key=lambda e: list(e.path))
        if problems:
            first = problems[0]
            where = "/".join(str(p) for p in first.path) or "arguments"
            detail = f"{where}: {first.message[:300]}"
            return self._checked(error_result("invalid_args", detail, self._meta(call, spec, started), max_chars=cap))
        if spec.meta.executor == "worker":
            return self._checked(await self._run_worker(call, spec, session, started))
        return self._checked(await self._run_host(call, spec, session, started))

    async def _run_worker(self, call: ToolCall, spec: ToolSpec, session: SessionCtx, started: float) -> dict[str, Any]:
        assert self.supervisor is not None
        check_id = "chk_" + new_ulid()
        deadline = spec.meta.timeout_ms or 5000
        conventions = {
            name: ({**value, "source": "pack"} if isinstance(value, dict) else {"value": value, "source": "pack"})
            for name, value in session.conventions.items()
        }
        # Chemistry constants use their symbol rather than the pack namespace in the worker protocol.
        conventions.update(
            {name.removeprefix("chem."): value for name, value in list(conventions.items()) if name.startswith("chem.")}
        )
        conventions.update(band_conventions(session.plausibility))
        args = dict(call.args)
        gravity = conventions.get("phy.g")
        if (
            "constants" in spec.params.get("properties", {})
            and "g" not in args.get("unknowns", [])
            and isinstance(gravity, dict)
            and "value" in gravity
        ):
            args["constants"] = [*args.get("constants", []), {"symbol": "g", **gravity, "source": "pack"}]
        outcome = await self.supervisor.call(spec.name, spec.version, args, check_id, deadline, call.call_id,
                                             conventions=conventions)  # fmt: skip
        if outcome.error == "worker_crashed" and spec.meta.idempotent and not spec.meta.side_effects:
            # AGT-27, TOL-26: the worker was respawned; a pure call is retried once on the new one, under a new check.
            check_id = "chk_" + new_ulid()
            outcome = await self.supervisor.call(spec.name, spec.version, args, check_id, deadline, call.call_id,
                                                 conventions=conventions)  # fmt: skip
        meta = self._meta(call, spec, started)
        if not outcome.ok:
            assert outcome.error is not None
            return error_result(outcome.error, outcome.detail, meta, max_chars=self.settings.tool_error_max_chars,
                                worker_code=outcome.data.get("worker_code"))  # fmt: skip
        assert outcome.result is not None
        record = outcome.result["check"]
        meta["checks"] = [{"check_id": record["check_id"], "tool": spec.name, "tool_version": spec.version,
                           "worker_version": record["worker_version"]}]  # fmt: skip
        # The model sees the check id with the value, so a plan can cite where each number came from (AGT-10), and
        # the worker's warnings, so the Checker can treat an `implausible` result as a probable input error (VER-26).
        shown = {"check_id": record["check_id"], "value": outcome.result["value"]}
        if outcome.result.get("warnings"):
            shown["warnings"] = outcome.result["warnings"]
        text = json.dumps(shown, ensure_ascii=False, sort_keys=True)
        return self._bounded(ok_result(text, meta, data=outcome.result), spec, session)

    async def _run_host(self, call: ToolCall, spec: ToolSpec, session: SessionCtx, started: float) -> dict[str, Any]:
        execute = self.host_executors[spec.name]
        last_beat = [time.monotonic()]
        context = CallContext(call.call_id, session, progress=lambda: last_beat.__setitem__(0, time.monotonic()))
        task: asyncio.Task[Any] = asyncio.create_task(execute(call.args, context))
        deadline = None if spec.meta.interactive else (spec.meta.timeout_ms or 1000) / 1000
        heartbeat = (spec.meta.heartbeat_ms or 0) / 1000
        try:
            while True:
                wait = heartbeat if heartbeat else deadline
                done, _ = await asyncio.wait({task}, timeout=wait)
                if done:
                    break
                elapsed = time.monotonic() - started
                if deadline is not None and elapsed >= deadline:
                    task.cancel()
                    return error_result("timeout", f"{spec.name} did not finish within {int(deadline * 1000)} ms",
                                        self._meta(call, spec, started))  # fmt: skip
                if heartbeat and time.monotonic() - last_beat[0] >= STALL_INTERVALS * heartbeat:
                    task.cancel()
                    return error_result("timeout", f"{spec.name} stopped reporting progress",
                                        self._meta(call, spec, started))  # fmt: skip
            value = task.result()
        except asyncio.CancelledError:
            task.cancel()  # the call was abandoned (TOL-14): its handler stops with it
            raise
        except InvalidArguments as refused:
            return error_result("invalid_args", str(refused)[:300], self._meta(call, spec, started))
        except Exception as error:  # a tool bug: report its type only (TOL-10)
            return error_result("execution_error", f"{spec.name} failed ({type(error).__name__})",
                                self._meta(call, spec, started))  # fmt: skip
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True)
        return self._bounded(ok_result(text, self._meta(call, spec, started), data=value), spec, session)

    def _bounded(self, result: dict[str, Any], spec: ToolSpec, session: SessionCtx) -> dict[str, Any]:
        profile_cap = (self.settings.tool_max_result_chars_small if session.profile == "small"
                       else self.settings.tool_max_result_chars_full)  # fmt: skip
        limit = min(spec.meta.max_result_chars.get(session.profile, profile_cap), profile_cap)
        output, truncated = bound(result["output"], limit, self.spill, session.session_id)
        result["output"] = output
        if truncated:
            result["meta"]["truncated"] = truncated
        data_size = len(json.dumps(result.get("data"), ensure_ascii=False)) if "data" in result else 0
        if data_size > self.settings.tool_max_data_kb * 1024:
            result.pop("data")
            result["meta"].setdefault("truncated", {"omitted": data_size, "spill_ref": self.spill.save(
                session.session_id, json.dumps(result.get("data")))})  # fmt: skip
        return result

    @staticmethod
    def _checked(result: dict[str, Any]) -> dict[str, Any]:
        problems = envelope_errors(result)
        if problems:
            raise AssertionError(f"tool envelope breaks its contract: {problems[0]}")
        return result

    # --- batches ------------------------------------------------------------------------------------------

    async def run_batch(self, calls: list[ToolCall], role: str, session: SessionCtx) -> list[dict[str, Any]]:
        results: list[dict[str, Any] | None] = [None] * len(calls)
        pool = asyncio.Semaphore(self.settings.worker_pool_size)

        async def run(index: int) -> None:
            async with pool:
                results[index] = await self.run_one(calls[index], role, session)

        def meta_of(name: str) -> Any:
            spec = self.registry.specs.get(name)
            return spec.meta if spec else None

        for group in plan(calls, meta_of):
            await asyncio.gather(*(run(i) for i in group))
        return [r for r in results if r is not None]


def band_conventions(bands: tuple[dict[str, Any], ...]) -> dict[str, Any]:
    """A skill's plausibility bands (quantity, unit, min, max) as the worker protocol's conventions: two entries per
    band, `plausibility.<quantity>.min` and `.max`, each `{value, unit, source}` (VER-26)."""
    conventions: dict[str, Any] = {}
    for band in bands:
        for end in ("min", "max"):
            conventions[f"plausibility.{band['quantity']}.{end}"] = {"value": band[end], "unit": band["unit"],
                                                                     "source": "pack"}  # fmt: skip
    return conventions


def _schema_sha256(schema: dict[str, Any]) -> str:
    canonical = json.dumps(schema, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode()).hexdigest()
