"""The runtime's launch attempts (MDL-16 to MDL-19): the verified model the shell starts, and the next smaller one.

Each verified model's GGUF header is read (under the parser rules of SEC-24) and the highest context rung that fits
memory and the class cap is chosen (MDL-17, MDL-18); a model that cannot fit the agent minimum is left out. The
largest model that fits is the first attempt and the next smaller one the single fallback the shell may try after the
restart limit (MDL-19). Arguments are `launch_arguments` without the port and the key file, which the shell sets for
each launch. This module only reads files; the core writes the plan and the shell acts on it."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_core.config import Settings
from agent_core.providers.gguf import GgufError, read_header
from agent_core.router.fit import choose_context, estimate_bytes, launch_arguments

PER_LAUNCH = ("--port", "--api-key-file")  # set by the shell for each launch, never in the plan
MAX_ATTEMPTS = 2  # the chosen model and one next smaller model (MDL-19)


@dataclass(frozen=True)
class Installed:
    model: str  # the runtime's model id (its folder name in the runtime's models folder)
    weights: Path
    projector_bytes: int = 0
    extras_bytes: int = 0  # side features' memory that runs with this model (MDL-17, `fit.feature_bytes`)


def without_per_launch(args: Sequence[str]) -> list[str]:
    out: list[str] = []
    skip = False
    for arg in args:
        if skip:
            skip = False
        elif arg in PER_LAUNCH:
            skip = True
        else:
            out.append(arg)
    return out


def attempts(installed: Sequence[Installed], hw_class: str, ram_bytes: int, local: Mapping[str, Any],
             settings: Settings, models_dir: Path, available_bytes: int | None = None,
             slot_dir: Path | None = None) -> tuple[list[dict[str, Any]], list[tuple[str, str]]]:  # fmt: skip
    """The launch attempts, largest model first, and the models left out with why. `available_bytes` sizes the
    context from the memory free now (ARC-31)."""
    fitting: list[tuple[int, dict[str, Any]]] = []
    left_out: list[tuple[str, str]] = []
    slots = int(local["slots"].get(hw_class, 1))
    for entry in installed:
        try:
            info = read_header(entry.weights)
        except (GgufError, OSError) as error:
            left_out.append((entry.model, f"unreadable model header: {error}"))
            continue
        context = choose_context(info, ram_bytes, hw_class, local["ladder"], local["memory_headroom_gb"],
                                 local["min_agent_context"], settings, entry.projector_bytes, available_bytes,
                                 entry.extras_bytes)  # fmt: skip
        if context is None:
            left_out.append((entry.model, "does not fit the agent minimum context in this computer's memory"))
            continue
        args = launch_arguments(str(models_dir), context, 0, "", slots, settings=settings,
                                slot_dir=str(slot_dir) if slot_dir is not None else None)  # fmt: skip
        # The resident estimate at the chosen rung: voice checks its own memory budget against it (VOC-03).
        resident = estimate_bytes(info, context, settings, entry.projector_bytes, entry.extras_bytes)
        fitting.append((info.file_bytes, {"model": entry.model, "context": context, "args": without_per_launch(args),
                                          "resident_bytes": resident, "family": info.architecture}))  # fmt: skip
    fitting.sort(key=lambda pair: -pair[0])
    return [plan for _, plan in fitting[:MAX_ATTEMPTS]], left_out
