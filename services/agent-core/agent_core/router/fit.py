"""Memory fit and the explicit context rung (MDL-17 and MDL-18 subsets; the probe and full supervisor are M11).

Resident memory is estimated as weights + KV cache at the chosen context + runtime overhead + projector. A context
rung is usable when the estimate keeps the app the headroom below installed RAM and the rung is within the model's
native context and the hardware class cap. The context is always passed explicitly at launch, never left to the
server's default."""

from __future__ import annotations

from collections.abc import Sequence

from agent_core.config import Settings
from agent_core.providers.gguf import GgufInfo

GB = 1024**3


def hardware_class(installed_gb: float, discrete_gpu: bool = False, apple_silicon: bool = False) -> str:
    """TABLE MDL-16a by memory alone (the full probe, with measured speeds, is M11)."""
    if installed_gb < 8:
        return "content_only"
    if installed_gb < 16:
        return "low"
    return "high" if discrete_gpu or apple_silicon else "mid"


def class_cap(settings: Settings, hw_class: str) -> int:
    return {"low": settings.context_cap_low, "mid": settings.context_cap_mid, "high": settings.context_cap_high}.get(
        hw_class, 0
    )


# Bytes per cached value by KV cache type (llama.cpp block layouts: q8_0 is 34 bytes per 32 values).
KV_VALUE_BYTES = {"f16": 2.0, "bf16": 2.0, "f32": 4.0, "q8_0": 34 / 32, "q5_1": 24 / 32, "q5_0": 22 / 32,
                  "q4_1": 20 / 32, "q4_0": 18 / 32, "iq4_nl": 18 / 32}  # fmt: skip


def pool_tokens(context: int, settings: Settings) -> int:
    """The shared KV pool the runtime is launched with: the agent context plus the pinned role prefixes (D128)."""
    return context + settings.runtime_prefix_pool_tokens


def estimate_bytes(model: GgufInfo, context: int, settings: Settings, projector_bytes: int = 0,
                   extras_bytes: int = 0) -> int:  # fmt: skip
    """MDL-17: weights + KV cache for `context` and the prefix pool at the launch's cache type + runtime overhead +
    projector, plus `extras_bytes`: the resident memory of the features enabled for the profile that run beside the
    model (the speech pack, VOC-01, and the photo reader, INP-21; `feature_bytes`)."""
    overhead = int(settings.runtime_overhead_gb * GB)
    per_value = KV_VALUE_BYTES.get(settings.runtime_kv_cache_type, 2.0)
    kv = int(model.kv_bytes_per_token(1) * per_value * pool_tokens(context, settings))
    return model.file_bytes + kv + overhead + projector_bytes + extras_bytes


def feature_bytes(settings: Settings, hw_class: str, speech: bool = False, photo_reader: bool = False) -> int:
    """The resident memory of the enabled side features MDL-17 adds to the estimate: the speech pack (VOC-01) and
    the photo reader's extra memory while reading (TABLE INP-29a, by class)."""
    index = {"low": 0, "mid": 1, "high": 2}.get(hw_class, 1)
    reader = settings.photo_reader_resident_gb[index] if photo_reader else 0.0
    voice = settings.speech_pack_resident_gb[index] if speech else 0.0
    return int((voice + reader) * GB)


def choose_context(
    model: GgufInfo,
    installed_bytes: int,
    hw_class: str,
    ladder: Sequence[int],
    headroom_gb: float,
    min_agent_context: int,
    settings: Settings,
    projector_bytes: int = 0,
    available_bytes: int | None = None,
    extras_bytes: int = 0,
) -> int | None:
    """The highest rung that fits, or None when even the agent minimum does not (the model is then not used for
    tutoring roles). Fitting means the estimate keeps the app `headroom_gb` below installed memory (MDL-17). When the
    memory free right now is known, the rung is also sized from it (ARC-31): the highest fitting rung whose estimate
    is within what is free, or the lowest fitting rung when none is (the model choice itself rests on installed
    memory, never on a passing moment)."""
    budget = installed_bytes - int(headroom_gb * GB)
    cap = class_cap(settings, hw_class)
    native = model.native_context or max(ladder)
    fitting = [r for r in ladder if r <= cap and r <= native and r >= min_agent_context
               and estimate_bytes(model, r, settings, projector_bytes, extras_bytes) <= budget]  # fmt: skip
    if not fitting:
        return None
    if available_bytes is not None:
        free_now = [r for r in fitting if estimate_bytes(model, r, settings, projector_bytes, extras_bytes)
                    <= available_bytes]  # fmt: skip
        return max(free_now) if free_now else min(fitting)
    return max(fitting)


def launch_arguments(
    models_dir: str,
    context: int,
    port: int,
    api_key_file: str,
    slots: int,
    models_max: int = 1,
    thinking_budget: int | None = None,
    settings: Settings | None = None,
    slot_dir: str | None = None,
) -> list[str]:
    """llama-server in router mode on loopback with the launch's key and an explicit context size. The class's
    thinking budget (MDL-12, `router_thinking_budget`) is fixed at launch, as the context and slots are.

    With `settings` (D128) the runtime gets one slot per pinned role in a unified KV pool: `--ctx-size` is the pool
    (`pool_tokens`) and every request is still limited to `context` (`--kv-unified-per-slot`). The host prompt cache
    is off and idle slots are never moved to it, so a role's prefix stays in its own slot and an erase clears every
    token (DAT-14); `slot_dir` enables the slot erase action. `slots` stays the concurrency the core's scheduler
    allows (MDL-22), not the runtime's slot count."""
    budget = ["--reasoning-budget", str(thinking_budget)] if thinking_budget is not None else []
    if settings is not None:
        size = ["--ctx-size", str(pool_tokens(context, settings)), "--kv-unified-per-slot", str(context)]
        parallel = ["--parallel", str(max(slots, settings.runtime_cache_slots)), "--kv-unified",
                    "--no-cache-idle-slots", "--cache-ram", "0",
                    "--cache-type-k", settings.runtime_kv_cache_type, "--cache-type-v", settings.runtime_kv_cache_type,
                    *(["--slot-save-path", slot_dir] if slot_dir else [])]  # fmt: skip
    else:
        size, parallel = ["--ctx-size", str(context)], ["--parallel", str(slots)]
    return [
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--api-key-file",
        api_key_file,
        "--models-dir",
        models_dir,
        "--models-max",
        str(models_max),
        *size,
        *parallel,
        "--no-webui",
        "--no-context-shift",  # an overflow surfaces as context_overflow, never a silently dropped system prompt
        "--jinja",
        "--reasoning-format",
        "deepseek",
        *budget,
    ]
