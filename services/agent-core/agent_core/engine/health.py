"""What the health panel reads (OBS-15) from the API it already has.

`GET /v1/health` carries a map of component to `ok`, `degraded`, `down` or `starting`, so each condition the panel
names is a component of its own; the client turns each problem component into one plain line with one action:

    worker            the tool worker answers (`down` while its circuit is open or no worker started)
    sandbox           `degraded` when a worker runs without its OS sandbox (SEC-06)
    keychain          `degraded` unless the shell keeps every device secret in the OS keychain (SEC-17, D97): the
                      file fallback, a secret the shell could not store, or a development build's memory store
    runtime_fallback  `degraded` when the runtime serves the next smaller model, not the plan's first (MDL-19)
    runtime_variant   `degraded` when the runtime runs on a llama.cpp build other than the first choice because that
                      one failed its touch generation (PLT-04); the build and why are in the hardware report
    class_lower       `degraded` when more than `class_suggest_lower_share` of the window's guided steps ran over the
                      class's budget: the panel suggests a lower class (PLT-09, `router.hardware.suggest_lower`)
    speed             `degraded` when the median guided step or first-words time of the last `health_window_days`
                      is over the hardware class's budget (AGT-28), once `health_min_turns` turns are recorded

`runtime`, `storage`, `capture` and `learner_store` are set elsewhere (MDL-19, DAT-13). `GET /v1/models` carries the
hardware class and why (`recommended.reason_key`), and the installed models with the model pack's version. The
hardware report (`router.hardware.report`: the failed minimum-laptop checks with their figures, the benchmark, the
student's class choice, the tutor's path and the runtime build with its fallbacks) is `ModelCatalogue.hardware`
(PLT-08, PLT-29; D76); it is sent when the bundled contract declares it.
"""

from __future__ import annotations

import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from agent_core.config import Settings

CLASSES = ("low", "mid", "high")  # the budget tuples' order (AGT-28)
DAY_MS = 86_400_000
RECHECK_MS = 3_600_000  # how far the window may slide before the speed is read again with no new trace


@dataclass(frozen=True)
class Speed:
    """Medians of the window's completed guided turns against the class budgets (s)."""

    turns: int
    step_s: float | None
    first_words_s: float | None
    step_budget_s: float
    first_words_budget_s: float
    min_turns: int

    @property
    def slow(self) -> bool:
        if self.turns < self.min_turns:
            return False  # too few turns to name the computer slow
        over_step = self.step_s is not None and self.step_s > self.step_budget_s
        over_first = self.first_words_s is not None and self.first_words_s > self.first_words_budget_s
        return over_step or over_first


def speed(rows: Sequence[dict[str, Any]], hardware_class: str | None, settings: Settings) -> Speed | None:
    """The speed condition from the trace store's timing rows (`TraceStore.timings_since`); None for a class with no
    budget (content-only, or no class yet). First words is the first model call phase's start plus the first Coach
    call's time to first token."""
    if hardware_class not in CLASSES:
        return None
    index = CLASSES.index(hardware_class)
    guided = [r for r in rows if (r.get("kind"), r.get("mode"), r.get("status")) == ("turn", "guided", "complete")]
    steps = [float(r["duration_ms"]) / 1000 for r in guided if r.get("duration_ms") is not None]
    first = [
        (float(r["first_call_offset_ms"]) + float(r["coach_ttft_ms"])) / 1000
        for r in guided
        if r.get("first_call_offset_ms") is not None and r.get("coach_ttft_ms") is not None
    ]
    return Speed(
        turns=len(steps),
        step_s=statistics.median(steps) if steps else None,
        first_words_s=statistics.median(first) if first else None,
        step_budget_s=settings.guided_step_budget_s[index],
        first_words_budget_s=settings.coach_first_text_budget_s[index],
        min_turns=settings.health_min_turns,
    )


def window_start_ms(now_ms: int, settings: Settings) -> int:
    return now_ms - settings.health_window_days * DAY_MS


def contract_carries_hardware() -> bool:
    """Whether the bundled commands contract declares `ModelCatalogue.hardware` (a proposed addition until then)."""
    from agent_core.contracts import document

    return "hardware" in document("api/commands")["$defs"]["ModelCatalogue"].get("properties", {})


def catalogue(plan: dict[str, Any] | None, serving: str | None,
              hardware: dict[str, Any] | None = None) -> dict[str, Any]:  # fmt: skip
    """`GET /v1/models` (ModelCatalogue) from the launch plan: the verified models with the model pack's version,
    the one the runtime serves for every tier, and the class's recommendation with why (`reason_key`). Downloads and
    the available list arrive with model packs (PLT-10, M20)."""
    plan = plan or {}
    pack = plan.get("model_pack") or {}
    used = {a["model"] for a in plan.get("attempts", [])}
    installed: list[dict[str, Any]] = [
        {"model_id": model, "pack_id": pack.get("pack_id"), "pack_version": pack.get("version"),
         "serving": model == serving, "planned": True}
        for model in [a["model"] for a in plan.get("attempts", [])]
    ]  # fmt: skip
    installed += [
        {"model_id": name, "pack_id": pack.get("pack_id"), "pack_version": pack.get("version"), "serving": False,
         "planned": False, "left_out": str(why)[:200]}
        for name, why in plan.get("left_out", [])
        if name not in used and name not in ("manifest.json", "plan")
    ]  # fmt: skip
    if serving and serving not in used:
        # A runtime the shell attached without a launch plan (the browser host's Glimmer): the tutor model is here.
        installed.append({"model_id": serving, "pack_id": pack.get("pack_id"), "pack_version": pack.get("version"),
                          "serving": True, "planned": False})  # fmt: skip
    if plan.get("hardware_class"):
        reason = f"hardware_class.{plan['hardware_class']}.{plan.get('hardware_reason') or 'predicted'}"
    elif hardware is not None and hardware.get("hardware_class") == "content_only":
        # Below the minimum laptop with no model pack at all: the class is still content-only, and why (PLT-29).
        reason = f"hardware_class.content_only.{hardware.get('basis') or 'predicted'}"
    else:
        reason = f"no_class.{plan.get('reason') or 'unknown'}"
    recommended = plan.get("model")
    out: dict[str, Any] = {
        "installed": installed,
        "available": [],
        "assignment": {"fast": serving, "strong": serving},
        "recommended": {"fast": recommended, "strong": recommended, "reason_key": reason[:80]},
    }
    if hardware is not None and contract_carries_hardware():
        out["hardware"] = hardware
    return out
