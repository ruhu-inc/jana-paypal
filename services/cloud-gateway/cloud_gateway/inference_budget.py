"""Deployment-wide inference caps at the provider boundary, shared by tutor, Study, Ask and probes.

Every request reserves a conservative byte/token upper bound in SQLite before calling the adapter: text by its UTF-8
bytes, each image at a fixed input-token allowance (images are billed as input tokens). Output
tokens (including reasoning) are explicitly bounded. A successful response with usage replaces the reservation
with actual cost. A caller that goes away before usage arrives (voice barge-in, a cancelled speculative Solver) is
charged its input bound plus the output delivered, outcome `client_disconnected` (SPEC §4.3): Meta may omit usage on an
interrupted stream (SPEC §16), and a hold kept at the full bound would lock the caps. Missing usage and failures retain
the bound until operator reconciliation; holds a crashed gateway left in flight are marked `unknown` at the next start.
No prompt, reply, account identifier or credential is stored here. Daily aggregate counters survive metadata purge;
unresolved calls remain held across midnight. This ledger covers inference, not external hosting/voice/search fees.

Each hold also has a row in `inference_budget_holds` saying when it was made, for which role and model, and how its
call ended (`OUTCOMES`), so an operator can release the holds of calls that are over (`release_held`) without ever
touching a call still in flight. The row carries no payload, pseudonym or account. A settle always records the
billed amount, even for a hold an operator already released, so a release can never lose real spend.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import secrets
from collections.abc import AsyncGenerator, Callable
from dataclasses import replace
from typing import Any

from cloud_gateway.config import GatewayConfig
from cloud_gateway.providers.base import (
    ProviderAdapter,
    ProviderReply,
    ProviderRequest,
    StreamPart,
    UpstreamError,
    Usage,
)
from cloud_gateway.quotas import cost_usd
from cloud_gateway.store import GatewayStore, day_of

MICROS = 1_000_000
SCHEMA = """
CREATE TABLE IF NOT EXISTS inference_budget_days (day TEXT PRIMARY KEY, spent_micros INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS inference_budget_pending (id TEXT PRIMARY KEY, reserved_micros INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS inference_budget_state (singleton INTEGER PRIMARY KEY CHECK(singleton=1));
CREATE TABLE IF NOT EXISTS inference_budget_holds (
    id TEXT PRIMARY KEY, reserved_micros INTEGER NOT NULL, created_at REAL NOT NULL, migrated INTEGER NOT NULL,
    role TEXT NOT NULL, model TEXT NOT NULL, outcome TEXT NOT NULL, status INTEGER, resolved_at REAL,
    released_at REAL, spent_micros INTEGER);
"""
# How a held call ended. `inference_budget_pending` stays the set of holds counted against the caps; these say why.
IN_FLIGHT = "in_flight"  # the call has not ended; never released by an operator
SETTLED = "settled"  # usage known: the hold was replaced by the billed amount
UNBILLED = "unbilled"  # refused before processing (401/403/429, no key): released at once
FAILED_UNBILLED_UNKNOWN = "failed_unbilled_unknown"  # another 4xx (e.g. 400): SPEC §16 does not say whether it bills
FAILED_OTHER = "failed_other"  # a 5xx, a network failure or an unclassified error: it may have been processed
TIMED_OUT = "timed_out"  # the provider timed out (408/504, or the client's timeout)
CANCELLED = "cancelled"  # the caller went away (task cancelled, stream closed early)
NO_USAGE = "no_usage"  # the call ended without usable usage counters
UNKNOWN = "unknown"  # made before outcomes were recorded, or left in flight by a gateway that stopped
CLIENT_DISCONNECTED = "client_disconnected"  # the caller went away first: settled at the input bound plus output seen
BILLED = (SETTLED, CLIENT_DISCONNECTED)  # a hold replaced by an amount: never settled twice
ENDED = (FAILED_UNBILLED_UNKNOWN, FAILED_OTHER, TIMED_OUT, CANCELLED, NO_USAGE)
# What `release_held` accepts. In-flight holds are never selectable; migrated (`unknown`) ones only by name.
RELEASABLE = {
    "failed": (FAILED_UNBILLED_UNKNOWN, FAILED_OTHER),
    "ended": ENDED,
    **{outcome: (outcome,) for outcome in (*ENDED, UNKNOWN)},
}
# The counted holds a release selects: outcome in a JSON list, made by a time, and of a status if one is given.
SELECTED = (
    "SELECT p.id, p.reserved_micros FROM inference_budget_pending p JOIN inference_budget_holds h ON h.id=p.id "
    "WHERE h.outcome IN (SELECT value FROM json_each(?)) AND h.created_at <= ? AND (? IS NULL OR h.status = ?)"
)


def input_bound(body: dict[str, Any], image_tokens: int) -> int:
    """The most input tokens a chat body can bill. UTF-8 bytes bound text-token counts conservatively (a token is at
    least one byte); framing headroom is added per message plus a fixed tool/schema allowance, and each image is
    bounded by its token allowance (its encoded bytes say nothing about its cost). No cache discount is assumed."""
    images = 0
    measured_messages = []
    for message in body.get("messages", []):
        content = message.get("content")
        if isinstance(content, list):
            images += sum(1 for part in content if part.get("type") == "image_url")
            content = [part for part in content if part.get("type") == "text"]
        measured_messages.append({**message, "content": content})
    body_bytes = len(json.dumps({**body, "messages": measured_messages}, ensure_ascii=False).encode())
    return body_bytes + 4096 + 256 * len(body.get("messages", [])) + images * image_tokens


def output_bytes(chunk: dict[str, Any] | None) -> int:
    """The bytes a stream chunk delivered (each choice's delta): at least its visible output tokens."""
    choices = [c for c in (chunk or {}).get("choices") or [] if isinstance(c, dict)]
    return sum(len(json.dumps(c.get("delta") or {}, ensure_ascii=False).encode()) for c in choices)


def abandoned_usage(request: ProviderRequest, image_tokens: int, output: int = 0) -> Usage:
    """What a call whose caller went away before usage arrived is charged (SPEC §4.3): its input bound, plus the bytes
    of output delivered so far. Hidden reasoning is not seen, so this is a floor the input bound's headroom pads."""
    return Usage(input_bound(request.body, image_tokens), output)


def _running(pid: int | None) -> bool:
    """Another process with this id is running. This process's own id means an earlier one (a container restart
    reuses it): the caller has made no hold yet."""
    if pid is None or pid == os.getpid():
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # running as another user
    return True


def micros(usd: float) -> int:
    if not math.isfinite(usd) or usd < 0:
        raise ValueError("budget amount must be finite and nonnegative")
    return math.ceil(usd * MICROS)


class InferenceBudget:
    def __init__(self, config: GatewayConfig, store: GatewayStore, clock: Callable[[], float]) -> None:
        self.config, self.store, self.clock = config, store, clock
        self.daily = config.quotas.deployment_daily_spend_usd
        self.total = config.quotas.deployment_total_spend_usd
        # Validate limits before initializing any provider. Round costs up and approved limits down.
        self.daily_micros = math.floor(self.daily * MICROS) if self.daily is not None else None
        self.total_micros = math.floor(self.total * MICROS) if self.total is not None else None
        store._db.executescript(SCHEMA)
        with store._tx() as db:
            if not db.execute("SELECT 1 FROM inference_budget_state").fetchone():
                # Preserve known spend when enabling the ledger on an existing gateway. Only retained records
                # are available at first initialization; enable this cap before the first paid deployment call.
                for day, usd in db.execute("SELECT day,sum(cost_usd) FROM requests GROUP BY day").fetchall():
                    db.execute("INSERT INTO inference_budget_days VALUES (?,?)", (day, micros(usd)))
                db.execute("INSERT INTO inference_budget_state VALUES (1)")
            if "pid" not in {row[1] for row in db.execute("PRAGMA table_info(inference_budget_holds)")}:
                db.execute("ALTER TABLE inference_budget_holds ADD COLUMN pid INTEGER")  # the process that made it
            # Holds made before outcomes were recorded (or by an older gateway still running against this file) get
            # an `unknown` row dated now, an upper bound on their age. They are never released unless named.
            db.execute(
                "INSERT INTO inference_budget_holds (id,reserved_micros,created_at,migrated,role,model,outcome) "
                "SELECT id,reserved_micros,?,1,'unknown','unknown',? FROM inference_budget_pending p "
                "WHERE NOT EXISTS (SELECT 1 FROM inference_budget_holds h WHERE h.id=p.id)",
                (self.clock(), UNKNOWN),
            )

    def recover(self) -> int:
        """At the serving gateway's start, before it makes a hold: a hold still in flight whose process is gone (killed
        or crashed mid-call, or made before holds named their process) can never end, so its outcome becomes
        `unknown`, released like a migrated hold only when named. A hold of a process still running is left alone, so
        the operator's command line, which also opens the ledger, never calls this. Returns how many were marked."""
        with self.store._tx() as db:
            rows = db.execute("SELECT id, pid FROM inference_budget_holds WHERE outcome=?", (IN_FLIGHT,)).fetchall()
            gone = [(self.clock(), key, IN_FLIGHT) for key, pid in rows if not _running(pid)]
            db.executemany("UPDATE inference_budget_holds SET outcome=?, resolved_at=? WHERE id=? AND outcome=?",
                           [(UNKNOWN, *row) for row in gone])  # fmt: skip
        return len(gone)

    def reserve(self, request: ProviderRequest) -> tuple[str, ProviderRequest]:
        expires = self.config.quotas.deployment_budget_expires_at
        if expires is not None and self.clock() >= expires.timestamp():
            raise UpstreamError("quota_or_billing", "approved deployment inference period has ended")
        price = self.config.prices.get(request.model.id)
        if price is None:
            raise UpstreamError("quota_or_billing", "no configured price for bounded inference")
        body = dict(request.body)
        for message in body.get("messages", []):
            content = message.get("content")
            if isinstance(content, list) and any(part.get("type") not in ("text", "image_url") for part in content):
                raise UpstreamError("format_error", "bounded inference accepts text and images only")
        limit = self.config.quotas.deployment_max_output_tokens
        for field in ("max_tokens", "max_completion_tokens"):
            if body.get(field) is not None:
                value = body[field]
                if type(value) is not int or value < 0:
                    raise UpstreamError("format_error", "invalid completion token limit")
                limit = min(limit, value)
        body.pop("max_tokens", None)
        body["max_completion_tokens"] = limit
        inputs = input_bound(body, self.config.quotas.deployment_image_input_tokens)
        bound = micros(cost_usd(price, Usage(inputs, limit)))
        return self._hold(bound, request.role, request.model.id), replace(request, body=body)

    def reserve_flat(self, usd: float, *, role: str = "unspecified", model: str = "unspecified") -> str:
        """A hold for a call priced per unit rather than per token (SAM 3.1 at $2.50 per 1,000 images): its exact
        cost is known before the send, so the hold is the cost. Settle it with `settle_flat`."""
        expires = self.config.quotas.deployment_budget_expires_at
        if expires is not None and self.clock() >= expires.timestamp():
            raise UpstreamError("quota_or_billing", "approved deployment inference period has ended")
        return self._hold(micros(usd), role, model)

    def _hold(self, bound: int, role: str, model: str) -> str:
        now = self.clock()
        key, day = secrets.token_hex(16), day_of(now)
        with self.store._tx() as db:
            held = db.execute("SELECT coalesce(sum(reserved_micros),0) FROM inference_budget_pending").fetchone()[0]
            spent = db.execute("SELECT coalesce(sum(spent_micros),0) FROM inference_budget_days").fetchone()[0]
            today = db.execute("SELECT spent_micros FROM inference_budget_days WHERE day=?", (day,)).fetchone()
            today_spent = today[0] if today else 0
            if (self.total_micros is not None and spent + held + bound > self.total_micros) or (
                self.daily_micros is not None and today_spent + held + bound > self.daily_micros
            ):
                raise UpstreamError("quota_or_billing", "deployment inference budget exhausted")
            db.execute("INSERT INTO inference_budget_pending VALUES (?,?)", (key, bound))
            db.execute(
                "INSERT INTO inference_budget_holds (id,reserved_micros,created_at,migrated,role,model,outcome,pid) "
                "VALUES (?,?,?,0,?,?,?,?)",
                (key, bound, now, role, model, IN_FLIGHT, os.getpid()),
            )
        return key

    def settle_flat(self, key: str, usd: float) -> None:
        """Replace a flat hold with the amount the provider billed."""
        self._settle(key, micros(usd))

    def settle(self, key: str, request: ProviderRequest, usage: Usage) -> None:
        # A text request always has input tokens. Missing/invalid counters cannot establish actual spend.
        if usage.input_tokens <= 0 or usage.output_tokens < 0 or not 0 <= usage.cached_tokens <= usage.input_tokens:
            self._mark(key, NO_USAGE)
            return
        self._settle(key, micros(cost_usd(self.config.prices[request.model.id], usage)))

    def abandon(self, key: str, request: ProviderRequest, output: int = 0) -> None:
        """The caller went away before usage arrived: replace the hold with `abandoned_usage` (SPEC §4.3)."""
        usage = abandoned_usage(request, self.config.quotas.deployment_image_input_tokens, output)
        self._settle(key, micros(cost_usd(self.config.prices[request.model.id], usage)), CLIENT_DISCONNECTED)

    def _settle(self, key: str, amount: int, outcome: str = SETTLED) -> None:
        """Record the billed amount once per hold. A hold an operator released is still settled: real spend is
        never lost to a release (the hold's bound is not counted twice, since the release already dropped it)."""
        now = self.clock()
        with self.store._tx() as db:
            row = db.execute("SELECT outcome FROM inference_budget_holds WHERE id=?", (key,)).fetchone()
            dropped = db.execute("DELETE FROM inference_budget_pending WHERE id=?", (key,)).rowcount
            if (row is None and not dropped) or (row is not None and row[0] in BILLED):
                return  # already settled
            db.execute(
                "UPDATE inference_budget_holds SET outcome=?, resolved_at=?, spent_micros=? WHERE id=?",
                (outcome, now, amount, key),
            )
            db.execute(
                "INSERT INTO inference_budget_days VALUES (?,?) ON CONFLICT(day) "
                "DO UPDATE SET spent_micros=spent_micros+excluded.spent_micros",
                (day_of(now), amount),
            )

    def _mark(self, key: str, outcome: str, status: int | None = None) -> None:
        """Record how a call ended. Only an in-flight hold changes: a settled or already-ended one keeps its outcome."""
        with self.store._tx() as db:
            db.execute(
                "UPDATE inference_budget_holds SET outcome=?, status=?, resolved_at=? WHERE id=? AND outcome=?",
                (outcome, status, self.clock(), key, IN_FLIGHT),
            )

    def fail(self, key: str, error: BaseException, *, output_seen: bool = False) -> None:
        """Record why a call ended without usage. Only a refusal before processing (`unbilled`, before any output)
        releases its hold; any other failure keeps it until an operator, having checked billing, releases it."""
        if isinstance(error, UpstreamError) and error.extra.get("unbilled") and not output_seen:
            self.release(key)
        elif isinstance(error, UpstreamError) and error.reason == "timeout":
            self._mark(key, TIMED_OUT, error.upstream_status)
        elif isinstance(error, UpstreamError) and error.upstream_status and 400 <= error.upstream_status < 500:
            # SPEC §16 records Meta's 4xx errors but not whether a rejected request (e.g. a 400 for a recursive
            # schema) is billed, so the hold stays and the outcome says so instead of guessing.
            self._mark(key, FAILED_UNBILLED_UNKNOWN, error.upstream_status)
        elif isinstance(error, (asyncio.CancelledError, GeneratorExit)):
            self._mark(key, CANCELLED)
        else:
            self._mark(key, FAILED_OTHER, error.upstream_status if isinstance(error, UpstreamError) else None)

    def release(self, key: str) -> None:
        """Drop a reservation for a call the provider refused before processing it (it bills nothing)."""
        now = self.clock()
        with self.store._tx() as db:
            db.execute("DELETE FROM inference_budget_pending WHERE id=?", (key,))
            db.execute(
                "UPDATE inference_budget_holds SET outcome=?, resolved_at=?, released_at=? WHERE id=? AND outcome=?",
                (UNBILLED, now, now, key, IN_FLIGHT),
            )

    def release_held(
        self, selector: str, older_than_s: float, *, status: int | None = None, apply: bool = False
    ) -> tuple[int, float]:
        """Operator reconciliation: the holds still counted whose call ended with an outcome `selector` names
        (`RELEASABLE`), made at least `older_than_s` ago, and, if given, with this upstream status. A dry run (the
        default) only counts them; `apply` releases them in the same transaction, so a call that is still in flight,
        or settles meanwhile, is never touched. Returns the count and the dollars held."""
        if selector not in RELEASABLE:
            raise ValueError(f"cannot release {selector!r}; choose one of {', '.join(RELEASABLE)}")
        now = self.clock()
        params = (json.dumps(RELEASABLE[selector]), now - older_than_s, status, status)
        with self.store._tx() as db:
            selected = db.execute(SELECTED, params).fetchall()
            if apply:
                db.executemany(
                    "UPDATE inference_budget_holds SET released_at=? WHERE id=?", [(now, i) for i, _ in selected]
                )
                db.executemany("DELETE FROM inference_budget_pending WHERE id=?", [(i,) for i, _ in selected])
        return len(selected), sum(held for _, held in selected) / MICROS

    def holds(self) -> list[dict[str, Any]]:
        """The holds still counted, grouped by outcome, role, model and status: count, dollars and oldest time."""
        with self.store._lock:
            rows = self.store._db.execute(
                "SELECT coalesce(h.outcome,?), coalesce(h.role,'unknown'), coalesce(h.model,'unknown'), h.status, "
                "count(*), sum(p.reserved_micros), min(h.created_at), max(coalesce(h.migrated,1)) "
                "FROM inference_budget_pending p LEFT JOIN inference_budget_holds h ON h.id=p.id "
                "GROUP BY 1,2,3,4 ORDER BY 6 DESC",
                (UNKNOWN,),
            ).fetchall()
        return [
            {"outcome": outcome, "role": role, "model": model, "status": status, "count": count,
             "held_usd": held / MICROS, "oldest_at": oldest, "migrated": bool(migrated)}
            for outcome, role, model, status, count, held, oldest, migrated in rows
        ]  # fmt: skip

    def purge(self, now: float, retention_days: int) -> None:
        """Drop the outcome rows of holds no longer counted (settled or released) once past metadata retention."""
        with self.store._tx() as db:
            db.execute(
                "DELETE FROM inference_budget_holds WHERE coalesce(resolved_at, created_at) < ? "
                "AND NOT EXISTS (SELECT 1 FROM inference_budget_pending p WHERE p.id=inference_budget_holds.id)",
                (now - retention_days * 86400,),
            )

    def snapshot(self) -> dict[str, Any]:
        with self.store._lock:
            db = self.store._db
            held = db.execute("SELECT coalesce(sum(reserved_micros),0) FROM inference_budget_pending").fetchone()[0]
            spent = db.execute("SELECT coalesce(sum(spent_micros),0) FROM inference_budget_days").fetchone()[0]
            today = db.execute(
                "SELECT spent_micros FROM inference_budget_days WHERE day=?", (day_of(self.clock()),)
            ).fetchone()
        return {
            "spent_usd": spent / MICROS,
            "today_usd": (today[0] if today else 0) / MICROS,
            "held_usd": held / MICROS,
        }


class BudgetedAdapter:
    """One wrapper for every configured inference adapter; no service can bypass the shared cap."""

    def __init__(self, adapter: ProviderAdapter, budget: InferenceBudget) -> None:
        self.adapter, self.budget = adapter, budget

    async def complete(self, request: ProviderRequest) -> ProviderReply:
        key, bounded = self.budget.reserve(request)
        try:
            reply = await self.adapter.complete(bounded)
        except asyncio.CancelledError:  # the caller went away (or timed out) before the reply
            self.budget.abandon(key, bounded)
            raise
        except BaseException as error:
            self.budget.fail(key, error)
            raise
        self.budget.settle(key, bounded, reply.usage)
        return reply

    async def stream(self, request: ProviderRequest) -> AsyncGenerator[StreamPart]:
        key, bounded = self.budget.reserve(request)
        parts, usage, ended, delivered = self.adapter.stream(bounded), None, False, 0
        try:
            async for part in parts:
                if part.usage is not None:
                    usage = part.usage
                delivered += output_bytes(part.chunk)
                yield part
            ended = True
            self.budget.settle(key, bounded, usage or Usage())  # no usage: the hold stays, marked `no_usage`
        except BaseException as error:
            # Bookkeeping first, without awaiting: a cancelled caller's next await may be cancelled again.
            cancelled = isinstance(error, (asyncio.CancelledError, GeneratorExit))  # a cancelled task or closed stream
            if not ended and not cancelled:
                self.budget.fail(key, error, output_seen=usage is not None)
            elif not ended and usage is not None:
                self.budget.settle(key, bounded, usage)  # the caller left after the usage chunk arrived
            elif not ended:
                self.budget.abandon(key, bounded, delivered)
            raise
        finally:
            await parts.aclose()

    async def aclose(self) -> None:
        await self.adapter.aclose()
