"""Model-proposed quantity mappings for `aut quantities --propose` (CUR-18, D90; the owner's decision of 2026-10-16).

Where no deterministic reading of an item binds a symbol a listed signature uses (`quantities.unbound`), a model may
be asked for the item's correct-answer formula written in symbols and the number it gives each symbol. The proposal
decides nothing: the extractor binds a symbol only where the item's own computation proves the formula with those
numbers, and every check of `pack_builder.quantities` then applies (`quantities.model_bindings`). Nothing unverified
is written.

What leaves the machine is the item's own stem, its numeric parts (prompt, key value, unit and check), its worked
solution with its checks, and the names of the symbols wanted; never its id, review or authoring data, misconception
records or anything else. A request whose text matches a held-out or safety-set item is blocked before it is sent
(AUT-20). Every request is audited (AUT-02) and listed in the egress log with its task, model and size (AUT-20).

Proposals are cached in a file, keyed by the sha256 of the request, with the spend so far: a rerun costs nothing, and
the budget (`quantity_proposal_budget_usd`) holds over every run against that cache. Before a request is sent its
worst case (the input at one token per three bytes, the full output allowance) must fit what is left of the budget;
once answered, the spend is counted from the tokens the provider reports, at the price the cloud gateway's
configuration lists for the model. The model is the gateway's fast tier on its primary provider (D7).
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import threading
import time
import tomllib
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent_core.config import Settings

from pack_builder import drafting
from pack_builder.accounts import AuditLog, home, now_iso
from pack_builder.heldout import HeldOut
from pack_builder.quantities import _check_expression, _text

TASK = "quantity_mapping"
KEY_ENV = "JANA_DEV_GEMINI_API_KEY"  # the developer's own key (cloud-gateway custody, `dev` only)
GATEWAY_DEFAULTS = Path(__file__).resolve().parents[2] / "cloud-gateway" / "cloud_gateway" / "defaults.toml"
TIER = "fast-model"
BYTES_PER_TOKEN = 3  # a deliberately low reading of bytes per token, so the input estimate errs high
RETRIES = (2.0, 8.0)  # seconds before each retry of a rate-limited or failed request


class BudgetSpent(RuntimeError):
    """The budget cannot cover another request."""


@dataclass(frozen=True)
class Model:
    """The model a proposal comes from, and its price per million tokens (USD)."""

    provider: str
    model_id: str
    base_url: str
    thinking_level: str | None
    input_per_m: float
    output_per_m: float

    def cost(self, input_tokens: int, output_tokens: int) -> float:
        return (input_tokens * self.input_per_m + output_tokens * self.output_per_m) / 1_000_000


def gateway_model(path: Path = GATEWAY_DEFAULTS, tier: str = TIER) -> Model:
    """The primary provider's model for `tier` as the cloud gateway configures it, with its listed price."""
    config = tomllib.loads(path.read_text())
    try:
        provider = config["slots"]["primary"]
        settings = config["providers"][provider]
        model = settings["models"][tier]
        price = config["prices"][model["id"]]
        adapter = settings["adapter"]
        permission = settings["permission"]
        expiry = datetime.fromisoformat(permission["expires_at"].replace("Z", "+00:00"))
        permitted = (
            bool(str(permission["reference"]).strip())
            and {"school", "adult"} <= set(permission["audiences"])
            and expiry > datetime.now(UTC)
        )
        prices = (price["input"], price["output"])
        valid_prices = all(
            isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0
            for value in prices
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError(
            "quantity proposals need an explicit gateway configuration with a selected model, "
            "prices and current whole-product service permission (--gateway-config)"
        ) from error
    if not permitted or not valid_prices:
        raise ValueError(
            "quantity proposals require finite non-negative prices and current whole-product service permission"
        )
    if adapter != "gemini":
        raise ValueError("quantity proposals require a Gemini provider; the configured provider is unsupported")
    thinking = model.get("thinking") or {}
    return Model(adapter, str(model["id"]), str(settings["base_url"]), thinking.get("low"),
                 float(price["input"]), float(price["output"]))  # fmt: skip


def api_key(env_file: Path | None = None) -> str:
    """The developer key, from the environment or else from a dotenv file; never printed, logged or stored."""
    found = os.environ.get(KEY_ENV)
    if not found and env_file is not None and env_file.is_file():
        for line in env_file.read_text().splitlines():
            name, sep, value = line.strip().removeprefix("export ").partition("=")
            if sep and name.strip() == KEY_ENV:
                found = value.strip().strip("'\"")
    if not found:
        raise RuntimeError(f"a model proposal needs the developer key {KEY_ENV} (environment or --env-file)")
    return found


# --- the request ----------------------------------------------------------------------------------------------------


def payload(item: Mapping[str, Any], symbols: Iterable[str]) -> dict[str, Any]:
    """The item's own stem, numeric parts, keys and solution, and the symbol names wanted: nothing else."""
    parts = []
    for part in item.get("parts") or []:
        answer = part.get("answer") or {}
        if answer.get("kind") != "numeric":
            continue
        key = {"value": answer.get("value"), "unit": answer.get("unit"),
               "check": _check_expression(item, answer.get("check_id"))}  # fmt: skip
        parts.append({"id": part.get("id"), "prompt": _text(part.get("prompt")),
                      "key": {k: v for k, v in key.items() if v not in (None, "")}})  # fmt: skip
    steps = []
    for step in (item.get("solution") or {}).get("steps") or []:
        written = {"latex": step.get("latex"), "check": _check_expression(item, step.get("check_id"))}
        steps.append({k: v for k, v in written.items() if v not in (None, "")})
    return {"stem": _text(item.get("stem")), "parts": parts, "solution": steps, "symbols": sorted(symbols)}


def request_text(body: Mapping[str, Any]) -> str:
    return json.dumps(body, ensure_ascii=False, sort_keys=True)


def request_sha(model: Model, system: str, user: str) -> str:
    return hashlib.sha256(f"{model.model_id}\n{system}\n{user}".encode()).hexdigest()


def gemini_body(system: str, user: str, model: Model, max_output_tokens: int) -> dict[str, Any]:
    generation: dict[str, Any] = {"temperature": 0, "maxOutputTokens": max_output_tokens,
                                  "responseMimeType": "application/json"}  # fmt: skip
    if model.thinking_level:
        generation["thinkingConfig"] = {"thinkingLevel": model.thinking_level}
    return {"systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}], "generationConfig": generation}  # fmt: skip


Transport = Callable[[dict[str, Any]], dict[str, Any]]  # a request body -> the provider's reply


def gemini_transport(model: Model, key: str, timeout_s: float) -> Transport:
    """generateContent on the provider's API; the key travels in a header only, never in a URL."""
    if model.provider != "gemini":
        raise ValueError("quantity proposals require a Gemini provider; the configured provider is unsupported")
    url = f"{model.base_url.rstrip('/')}/models/{model.model_id}:generateContent"

    def send(body: dict[str, Any]) -> dict[str, Any]:
        headers = {"x-goog-api-key": key, "content-type": "application/json"}
        # The URL is the gateway configuration's https endpoint, never one a record or a person supplies.
        request = urllib.request.Request(url, json.dumps(body).encode(), headers=headers, method="POST")  # noqa: S310
        for attempt, pause in enumerate((*RETRIES, None)):
            try:
                with urllib.request.urlopen(request, timeout=timeout_s) as response:  # noqa: S310 - a fixed https URL
                    reply: dict[str, Any] = json.loads(response.read())
                    return reply
            except urllib.error.HTTPError as error:
                if pause is None or error.code not in (429, 500, 502, 503, 504):
                    raise ConnectionError(f"the model request failed with HTTP {error.code}: "
                                          f"{_provider_message(error)}") from None  # fmt: skip
            except (urllib.error.URLError, TimeoutError) as error:
                if pause is None:
                    raise ConnectionError(f"the model request failed: {type(error).__name__}") from None
            time.sleep(pause * (attempt + 1))
        raise ConnectionError("the model request failed")

    return send


def _provider_message(error: urllib.error.HTTPError) -> str:
    """The provider's own error message (it never echoes the key), shortened."""
    try:
        return str(json.loads(error.read())["error"]["message"])[:200]
    except (OSError, ValueError, KeyError, TypeError):
        return "no message"


def usage(reply: Mapping[str, Any]) -> tuple[int, int]:
    """Input and output tokens the provider reports (thinking tokens count as output)."""
    meta = reply.get("usageMetadata") or {}
    output = int(meta.get("candidatesTokenCount") or 0) + int(meta.get("thoughtsTokenCount") or 0)
    return int(meta.get("promptTokenCount") or 0), output


def parse(reply: Mapping[str, Any]) -> dict[str, Any] | None:
    """The proposal in a reply: formulas by part id and a number per symbol, as strings; None when malformed."""
    try:
        parts = reply["candidates"][0]["content"]["parts"]
        text = "".join(str(p.get("text", "")) for p in parts if not p.get("thought"))
        found = json.loads(drafting._strip_fence(text))
    except (KeyError, IndexError, TypeError, ValueError):
        return None
    if not isinstance(found, dict):
        return None
    formulas = found.get("formulas")
    quantities = found.get("quantities")
    formulas = formulas if isinstance(formulas, dict) else {}
    quantities = quantities if isinstance(quantities, dict) else {}
    return {"formulas": {str(k): str(v) for k, v in formulas.items() if isinstance(v, (str, int, float))},
            "quantities": {str(k): str(v) for k, v in quantities.items()
                           if isinstance(v, (str, int, float)) and not isinstance(v, bool)}}  # fmt: skip


# --- the cache and the budget ---------------------------------------------------------------------------------------


@dataclass
class Cache:
    """Proposals by item id with the request they answer, and the spend of every run against this file."""

    path: Path
    data: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.path.is_file():
            self.data = json.loads(self.path.read_text())
        self.data.setdefault("spend_usd", 0.0)
        self.data.setdefault("requests", 0)
        self.data.setdefault("tokens", {"input": 0, "output": 0})
        self.data.setdefault("entries", {})

    @property
    def spend(self) -> float:
        return float(self.data["spend_usd"])

    def hit(self, item_id: str, sha: str) -> dict[str, Any] | None:
        entry = self.data["entries"].get(item_id)
        return entry if entry is not None and entry.get("request_sha256") == sha else None

    def proposals(self) -> dict[str, dict[str, Any]]:
        return {k: v["proposal"] for k, v in self.data["entries"].items() if v.get("proposal")}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        ordered = {**self.data, "entries": dict(sorted(self.data["entries"].items()))}
        self.path.write_text(json.dumps(ordered, indent=1, sort_keys=True) + "\n")


@dataclass
class Run:
    """One proposal run's outcome."""

    asked: list[str] = field(default_factory=list)
    cached: list[str] = field(default_factory=list)
    blocked: list[str] = field(default_factory=list)  # held-out or safety-set match: nothing was sent (AUT-20)
    failed: dict[str, str] = field(default_factory=dict)
    unaffordable: list[str] = field(default_factory=list)  # the budget could not cover them
    spend_before: float = 0.0
    spend_after: float = 0.0

    def report(self, budget: float) -> dict[str, Any]:
        return {"asked": len(self.asked), "cached": len(self.cached), "blocked": self.blocked,
                "failed": self.failed, "not_asked_budget": len(self.unaffordable), "budget_usd": budget,
                "spend_usd_this_run": round(self.spend_after - self.spend_before, 6),
                "spend_usd_total": round(self.spend_after, 6)}  # fmt: skip


def propose(wanted: list[tuple[Mapping[str, Any], list[str]]], cache: Cache, model: Model, transport: Transport,
            forbidden: HeldOut, audit: AuditLog, actor: str, settings: Settings | None = None,
            egress_log: Path | None = None, workers: int = 1) -> Run:  # fmt: skip
    """Ask the model about each (item, symbols) the cache does not answer, within the budget; the cache is saved as
    each answer arrives."""
    settings = settings or Settings()
    budget = settings.quantity_proposal_budget_usd
    template = drafting.prompt(TASK)
    system = template.body
    egress = egress_log or home() / "egress.jsonl"
    run = Run(spend_before=cache.spend)
    lock = threading.Lock()
    reserved = [0.0]

    def one(item: Mapping[str, Any], symbols: list[str]) -> None:
        item_id = str(item["id"])
        user = request_text(payload(item, symbols))
        sha = request_sha(model, system, user)
        with lock:
            if cache.hit(item_id, sha) is not None:
                run.cached.append(item_id)
                return
        if forbidden.blocks(user) or forbidden.blocks(system):
            with lock:
                run.blocked.append(item_id)
                audit.append(actor, f"ai_propose:{TASK}", "blocked", item_id, "held-out or safety-set match")
            return
        worst = model.cost(-(-len((system + user).encode()) // BYTES_PER_TOKEN),
                           settings.quantity_proposal_max_output_tokens)  # fmt: skip
        with lock:
            if cache.spend + reserved[0] + worst > budget:
                run.unaffordable.append(item_id)
                return
            reserved[0] += worst
        body = gemini_body(system, user, model, settings.quantity_proposal_max_output_tokens)
        try:
            reply = transport(body)
            error = None
        except ConnectionError as failure:
            reply, error = {}, str(failure)
        tokens_in, tokens_out = usage(reply)
        cost = model.cost(tokens_in, tokens_out) if reply else 0.0
        proposal = parse(reply) if reply else None
        with lock:
            reserved[0] -= worst
            cache.data["spend_usd"] = cache.spend + cost
            cache.data["requests"] += 1
            cache.data["tokens"]["input"] += tokens_in
            cache.data["tokens"]["output"] += tokens_out
            with egress.open("a") as out:
                out.write(json.dumps({"at": now_iso(), "task": TASK, "model": model.model_id,
                                      "bytes": len(system.encode()) + len(user.encode())}) + "\n")  # fmt: skip
            outcome = "failed" if error or proposal is None else "ok"
            audit.append(actor, f"ai_propose:{TASK}", outcome, item_id,
                         f"{model.provider} {model.model_id} {tokens_in}+{tokens_out} tokens")  # fmt: skip
            if error or proposal is None:
                run.failed[item_id] = error or "the reply was not a proposal"
            else:
                cache.data["entries"][item_id] = {"request_sha256": sha, "proposal": proposal, "at": now_iso(),
                                                  "model": model.model_id, "provider": model.provider,
                                                  "prompt_id": template.prompt_id,
                                                  "prompt_sha256": template.sha256}  # fmt: skip
                run.asked.append(item_id)
            cache.save()

    egress.parent.mkdir(parents=True, exist_ok=True)
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for future in [pool.submit(one, item, symbols) for item, symbols in wanted]:
            future.result()
    run.spend_after = cache.spend
    return run


def field_record(entry: Mapping[str, Any], item_id: str, actor_rev: str) -> dict[str, Any]:
    """The field record of quantities a model proposed and the tools proved (AUT-19): AI origin with the model, its
    provider, tier and prompt, the item it read, verified by tool and accepted by the account that ran the pass (the
    owner's decision of 2026-10-16)."""
    return {"origin": "ai_accepted", "accepted": True, "accepted_by": actor_rev, "status": "passed",
            "model": entry.get("model"), "provider": entry.get("provider"), "tier": TIER,
            "prompt_id": entry.get("prompt_id"), "prompt_sha256": entry.get("prompt_sha256"), "inputs": [item_id],
            "at": entry.get("at"),
            "detail": "a model proposed the symbol mapping; the tools proved every quantity (CUR-18)"}  # fmt: skip
