"""Every configuration key, declared once (ARC-28): type, default, unit, range, the layers that may set it and the
requirement it serves. Proposal values from the specification are defaults here and never literals in code.

Types shared with a wire contract come from the generated bindings (D39), never restated here.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field
from pydantic.config import JsonDict

from agent_core.config.layers import Layer
from agent_core.contracts.api.commands import Settings as SettingsContract


def _contract_type(name: str) -> Any:
    """A Settings field's type from the generated contract, constraints included."""
    field = SettingsContract.model_fields[name]
    return Annotated[field.annotation, *field.metadata] if field.metadata else field.annotation


TextScale = _contract_type("text_scale")
Locale = _contract_type("locale")
# DSN-26, D103: the six tutor characters of TABLE DSN-26a (the contract's enum; dsn26_character_set_matches_contract).
TutorCharacter = Literal["avatar.jana", "avatar.orbit", "avatar.prism", "avatar.kite", "avatar.pebble", "avatar.ray"]

ENGINEERING = (Layer.DEFAULTS, Layer.HARDWARE, Layer.DEVELOPER)
ONLINE_FEATURES = (Layer.DEFAULTS, Layer.ONLINE_FEATURES, Layer.DEVELOPER, Layer.SAFETY)
STUDENT_PREFERENCE = (Layer.DEFAULTS, Layer.PROFILE, Layer.DEVELOPER)


def key(
    default: Any,
    *,
    serves: str,
    layers: tuple[Layer, ...],
    unit: str | None = None,
    ge: float | None = None,
    le: float | None = None,
    restrict: str | list[Any] | None = None,
    client: bool = False,
    desktop_shell: bool = False,
) -> Any:
    """A declared key. `restrict` says which direction is stricter for restrict-only layers: "lower", "higher",
    "false" (False is stricter) or an ordered list of values, strictest first. `client` marks keys the UI reads; the
    codegen writes their defaults into the client's generated key module, so the UI has no second source.
    `desktop_shell` marks the keys the Rust shell reads, generated into its constants the same way."""
    extra: JsonDict = {
        "serves": serves,
        "layers": [int(layer) for layer in layers],
        "unit": unit,
        "restrict": restrict,
        "client": client,
        "shell": desktop_shell,
    }
    return Field(default, ge=ge, le=le, json_schema_extra=extra)


class Settings(BaseModel):
    """The resolved configuration. Field names are the configuration keys."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    # --- online features with their consents (ARC-27 layer 5; ARC-11, D53, D86) ---
    online_cloud_tier: bool = key(False, serves="ARC-07", layers=ONLINE_FEATURES, restrict="false")
    online_downloads: bool = key(False, serves="ARC-07", layers=ONLINE_FEATURES, restrict="false")
    online_update_checks: bool = key(False, serves="ARC-07", layers=ONLINE_FEATURES, restrict="false")
    online_cloud_backup: bool = key(False, serves="ARC-07", layers=ONLINE_FEATURES, restrict="false")
    online_telemetry: bool = key(False, serves="ARC-07", layers=ONLINE_FEATURES, restrict="false")
    online_crash_reports: bool = key(False, serves="ARC-07", layers=ONLINE_FEATURES, restrict="false")

    # --- student preferences (layer 6) ---
    text_scale: TextScale = key(1.0, serves="DSN-07", layers=STUDENT_PREFERENCE)  # type: ignore[valid-type]
    locale: Locale = key("en", serves="LOC-06", layers=STUDENT_PREFERENCE)  # type: ignore[valid-type]
    low_power: bool = key(False, serves="PLT-22", layers=STUDENT_PREFERENCE)
    memory_saving: bool = key(True, serves="MEM-06", layers=STUDENT_PREFERENCE, restrict="false")
    notifications: bool = key(False, serves="LRN-35", layers=STUDENT_PREFERENCE)
    reduced_motion: bool = key(False, serves="LOC-20", layers=STUDENT_PREFERENCE)
    read_aloud: bool = key(False, serves="VOC-18", layers=STUDENT_PREFERENCE)
    speech_rate: float = key(1.0, serves="VOC-17", layers=STUDENT_PREFERENCE, ge=0.75, le=1.5)
    speech_voice: Annotated[str, Field(max_length=256)] = key("", serves="VOC-17", layers=STUDENT_PREFERENCE)
    easy_read: bool = key(False, serves="LOC-18", layers=STUDENT_PREFERENCE)
    extra_time_percent: int = key(0, serves="ASM-24", layers=STUDENT_PREFERENCE, ge=0, le=50)
    theme: Literal["system", "light", "dark"] = key("system", serves="DSN-05", layers=STUDENT_PREFERENCE)
    character_shortcuts: bool = key(True, serves="LOC-12", layers=STUDENT_PREFERENCE)
    # PRM-02, PRM-03, D47: the tutor's name and avatar for the profile; the defaults are the identity file's.
    tutor_name: Annotated[str, Field(min_length=1, max_length=40)] = key(
        "Jana", serves="PRM-03", layers=STUDENT_PREFERENCE
    )
    # DSN-26, D103: one of the six tutor characters of TABLE DSN-26a; Jana is the default (D47).
    tutor_avatar: TutorCharacter = key("avatar.jana", serves="PRM-03", layers=STUDENT_PREFERENCE)
    # UXS-03, MDL-05, D103: "Use the faster tutor", remembered; strong-tier roles are served from the fast tier.
    faster_tutor: bool = key(False, serves="MDL-05", layers=STUDENT_PREFERENCE)
    # VOC-49, retired by D126: the wake word is gone (a hands-free conversation starts from a button or Ctrl+Shift+V).
    # The key is kept so stored profiles and event history stay valid; it is ignored.
    wake_word: bool = key(False, serves="VOC-49", layers=STUDENT_PREFERENCE)
    # SPEC §19.3.4 (W8, JM-D24): a desktop notification for a due check-in, off by default; the Home card is always on.
    checkin_notifications: bool = key(False, serves="LRN-35", layers=STUDENT_PREFERENCE)

    # --- build and environment (OPS-35) ---
    environment: Literal["dev", "staging", "pilot", "production"] = key(
        "dev", serves="OPS-35", layers=(Layer.DEFAULTS,)
    )

    # Operator-exported, host-bound service/capability projection; no credentials or approval ship by default.
    cloud_catalogue_file: str = key("", serves="MDL-26", layers=ENGINEERING)

    # M42 capability gate; online processing still requires separate subscription/consent/DPIA gates.
    study_workspace_enabled: bool = key(False, serves="STU-01", layers=ENGINEERING)
    # D133: study tools are made online only (the paid online plan); local generation was too slow and thin on student
    # laptops. On, short documents may also be processed on the device, as before D133.
    study_offline_enabled: bool = key(False, serves="STU-17", layers=ENGINEERING)
    # Optional prototype narration uses Study consent, without microphone capture or the conversational voice path.
    study_neural_speech_enabled: bool = key(False, serves="STU-01", layers=ENGINEERING)
    # Cloud voice (VOC-28, S4) built ahead of its stage: the `cloud_voice` switch and consent scope exist only with
    # this capability on; using it still needs the switch, the profile's consent, the subscription and the notice.
    cloud_voice_enabled: bool = key(False, serves="VOC-28", layers=ENGINEERING)
    study_upload_staging_ttl_s: int = key(86400, serves="STU-19", layers=ENGINEERING, unit="s", ge=3600, le=604800)
    study_source_max_mb: int = key(100, serves="STU-02", layers=ENGINEERING, unit="MiB", ge=1, le=100)
    study_offline_max_pages: int = key(30, serves="STU-08", layers=ENGINEERING, unit="pages", ge=1, le=30)
    study_job_queue_limit: int = key(20, serves="STU-17", layers=ENGINEERING, unit="jobs", ge=1, le=20)
    study_batch_char_limit: int = key(48000, serves="STU-17", layers=ENGINEERING, unit="characters", ge=16000, le=48000)
    # UXS-67: a pasted text at least this long, and more statement than question, is material for the study area.
    ask_material_min_chars: int = key(400, serves="UXS-67", layers=ENGINEERING, unit="characters", ge=200, le=5000)

    # --- engineering values (proposals) ---
    net_connect_timeout_s: float = key(
        5.0, serves="ARC-06", layers=ENGINEERING, unit="s", ge=0.1, le=60, desktop_shell=True
    )
    # --- the connectivity layer (ARC-08, ARC-09, MDL-24, MDL-29; proposals) ---
    net_probe_interval_s: float = key(60.0, serves="ARC-08", layers=ENGINEERING, unit="s", ge=60, le=3600)
    net_probe_backoff_max_s: float = key(900.0, serves="ARC-08", layers=ENGINEERING, unit="s", ge=60, le=86400)
    outbox_backoff_min_s: float = key(30.0, serves="ARC-09", layers=ENGINEERING, unit="s", ge=1, le=3600)
    outbox_backoff_max_s: float = key(21600.0, serves="ARC-09", layers=ENGINEERING, unit="s", ge=60, le=86400)
    outbox_cap_mb: float = key(50.0, serves="ARC-09", layers=ENGINEERING, unit="MB", ge=1, le=4096)
    outbox_item_days: int = key(30, serves="ARC-09", layers=ENGINEERING, unit="days", ge=1, le=365)
    cloud_offline_added_wait_ms: int = key(200, serves="MDL-24", layers=ENGINEERING, unit="ms", ge=0, le=5000)
    # A Muse Spark tutor turn costs about $0.04-0.06 with its reasoning (measured 2026-10-26), so a session gets room
    # for 50 or more turns; the gateway's deployment ledger stays the hard limit on total spend.
    cloud_budget_session_usd: float = key(3.0, serves="MDL-29", layers=ENGINEERING, unit="USD", ge=0)
    cloud_budget_student_day_usd: float = key(10.0, serves="MDL-29", layers=ENGINEERING, unit="USD", ge=0)
    cloud_budget_reminders: tuple[float, ...] = key((0.5, 0.85), serves="MDL-29", layers=ENGINEERING, unit="share")
    cloud_disable_all_budget_s: float = key(5.0, serves="PRV-02", layers=ENGINEERING, unit="s", ge=0.1, le=60)
    startup_home_ready_budget_s: float = key(5.0, serves="ARC-22", layers=ENGINEERING, unit="s", ge=0.5, le=60)
    startup_model_warm_budget_s: float = key(60.0, serves="ARC-22", layers=ENGINEERING, unit="s", ge=1, le=600)
    api_major_overlap_minor_releases: int = key(2, serves="API-35", layers=ENGINEERING, unit="releases", ge=1, le=12)
    # --- tool worker (TOL-11, TOL-12, TOL-24, TOL-26, SEC-08) ---
    worker_pool_size: int = key(4, serves="TOL-11", layers=ENGINEERING, unit="workers", ge=1, le=8)
    worker_frame_limit_bytes: int = key(4 * 1024 * 1024, serves="TOL-24", layers=ENGINEERING, unit="bytes", ge=65536)
    worker_warm_target_s: float = key(3.0, serves="TOL-26", layers=ENGINEERING, unit="s", ge=0.1, le=60)
    # Cold import/OS startup may exceed the warm latency objective. Bound the handshake independently.
    worker_handshake_timeout_s: float = key(30.0, serves="TOL-26", layers=ENGINEERING, unit="s", ge=0.1, le=180)
    worker_ping_interval_s: float = key(5.0, serves="TOL-26", layers=ENGINEERING, unit="s", ge=0.05, le=60)
    worker_missed_pings_recycle: int = key(2, serves="TOL-26", layers=ENGINEERING, unit="pings", ge=1, le=10)
    worker_respawn_backoff_s: tuple[float, ...] = key(
        (0.5, 1.0, 2.0, 4.0), serves="TOL-26", layers=ENGINEERING, unit="s"
    )
    worker_circuit_restarts: int = key(5, serves="TOL-26", layers=ENGINEERING, unit="restarts", ge=1, le=100)
    worker_circuit_window_s: float = key(60.0, serves="TOL-26", layers=ENGINEERING, unit="s", ge=1, le=3600)
    worker_circuit_open_s: float = key(60.0, serves="TOL-26", layers=ENGINEERING, unit="s", ge=1, le=3600)
    worker_call_cpu_ms: int = key(2000, serves="SEC-08", layers=ENGINEERING, unit="ms", ge=100, le=30000)
    worker_call_memory_mb: int = key(256, serves="SEC-08", layers=ENGINEERING, unit="MB", ge=32, le=4096)
    worker_max_expression_chars: int = key(2000, serves="SEC-08", layers=ENGINEERING, unit="characters", ge=1)
    worker_max_data_points: int = key(500, serves="SEC-08", layers=ENGINEERING, unit="points", ge=1)
    worker_process_memory_mb: int = key(512, serves="SEC-08", layers=ENGINEERING, unit="MB", ge=64, le=8192)
    worker_max_open_files: int = key(64, serves="SEC-08", layers=ENGINEERING, unit="files", ge=8, le=4096)
    worker_max_file_mb: int = key(10, serves="SEC-08", layers=ENGINEERING, unit="MB", ge=1, le=1024)
    # TABLE SEC-06a (Windows): the job object's hard CPU rate cap, as a share of the whole machine. The table names
    # the cap without a value; half the machine keeps the interface responsive while a worker spins (engineering).
    worker_cpu_rate_percent: int = key(50, serves="SEC-06", layers=ENGINEERING, unit="percent", ge=1, le=100)
    # SEC-08: the perception worker's own limits (proposal: 1.5 GB on the mid class, 20 s per page).
    perception_memory_mb: int = key(1536, serves="SEC-08", layers=ENGINEERING, unit="MB", ge=256, le=8192)
    perception_page_s: float = key(20.0, serves="SEC-08", layers=ENGINEERING, unit="s", ge=1, le=300)
    # --- verification (VER-03, VER-04, VER-06) ---
    ver_max_depth: int = key(64, serves="VER-03", layers=ENGINEERING, unit="levels", ge=8, le=256)
    ver_equivalence_points: int = key(12, serves="VER-04", layers=ENGINEERING, unit="points", ge=4, le=200)
    ver_equivalence_min_valid: int = key(8, serves="VER-04", layers=ENGINEERING, unit="points", ge=2, le=200)
    ver_numeric_digits: int = key(30, serves="VER-04", layers=ENGINEERING, unit="digits", ge=15, le=100)
    ver_relative_tolerance: float = key(1e-9, serves="VER-04", layers=ENGINEERING, ge=1e-15, le=1e-3)
    ver_symbolic_slice_ms: int = key(800, serves="VER-04", layers=ENGINEERING, unit="ms", ge=50, le=10000)
    ver_step_candidates: int = key(64, serves="VER-06", layers=ENGINEERING, unit="candidates", ge=1, le=1000)
    # --- verification breadth, mathematics (M12.a: TABLE VER-09a defaults, VER-13, VER-14; a pack convention or the
    # item's marking rule overrides the VER-09 values per call) ---
    ver_default_sf: int = key(3, serves="VER-09", layers=ENGINEERING, unit="significant figures", ge=1, le=15)
    ver_min_sf: int = key(2, serves="VER-09", layers=ENGINEERING, unit="significant figures", ge=1, le=15)
    ver_carried_rounding_share: float = key(0.01, serves="VER-09", layers=ENGINEERING, unit="share", ge=0, le=0.5)
    ver_money_dp: int = key(2, serves="VER-09", layers=ENGINEERING, unit="decimal places", ge=0, le=6)
    ver_angle_dp: int = key(1, serves="VER-09", layers=ENGINEERING, unit="decimal places", ge=0, le=6)
    ver_angle_tolerance_deg: float = key(0.1, serves="VER-09", layers=ENGINEERING, unit="degrees", ge=0, le=10)
    ver_probability_sf: int = key(3, serves="VER-09", layers=ENGINEERING, unit="significant figures", ge=1, le=15)
    ver_stats_max_points: int = key(500, serves="VER-13", layers=ENGINEERING, unit="points", ge=1, le=500)
    ver_plot_max_points: int = key(500, serves="VER-14", layers=ENGINEERING, unit="points", ge=20, le=500)
    ver_plot_chord_tolerance: float = key(0.001, serves="VER-14", layers=ENGINEERING, unit="share", ge=1e-6, le=0.1)
    # --- verification breadth, accounting (M12.d): places shown when neither the call nor the pack states them; a
    # depreciation charge is rounded to them year by year (VER-24) ---
    ver_acc_dp: int = key(2, serves="VER-24", layers=ENGINEERING, unit="decimal places", ge=0, le=4)
    # --- performance budgets (M12.e, VER-28): the p95 latency of one call on the low hardware class reference laptop;
    # every call's CPU is capped by worker_call_cpu_ms and a started worker is warm within worker_warm_target_s ---
    ver_p95_math_parse_ms: int = key(20, serves="VER-28", layers=ENGINEERING, unit="ms", ge=1, le=30000)
    ver_p95_math_check_equivalence_ms: int = key(300, serves="VER-28", layers=ENGINEERING, unit="ms", ge=1, le=30000)
    ver_p95_math_check_step_ms: int = key(500, serves="VER-28", layers=ENGINEERING, unit="ms", ge=1, le=30000)
    ver_p95_chem_balance_ms: int = key(100, serves="VER-28", layers=ENGINEERING, unit="ms", ge=1, le=30000)
    ver_p95_acc_statements_ms: int = key(300, serves="VER-28", layers=ENGINEERING, unit="ms", ge=1, le=30000)
    ver_p95_phys_circuit_ms: int = key(200, serves="VER-28", layers=ENGINEERING, unit="ms", ge=1, le=30000)
    golden_min_cases_per_tool: int = key(100, serves="VER-29", layers=(Layer.DEFAULTS,), unit="cases", ge=1)
    golden_min_cases_per_subject_level: int = key(300, serves="VER-29", layers=(Layer.DEFAULTS,), unit="cases", ge=1)
    golden_min_seeded_step_pairs: int = key(500, serves="VER-29", layers=(Layer.DEFAULTS,), unit="pairs", ge=1)

    # --- prompts (PRM-09, PRM-04; character budgets for tiers 1-8 by prompt mode; tier 9 is the remainder) ---
    prompt_budget_full: tuple[int, ...] = key(
        (2000, 3000, 3000, 1000, 8000, 2400, 300, 2000), serves="PRM-09", layers=ENGINEERING, unit="characters"
    )
    prompt_budget_compact: tuple[int, ...] = key(
        (1700, 2000, 800, 600, 4800, 1600, 300, 1600), serves="PRM-09", layers=ENGINEERING, unit="characters"
    )
    prompt_budget_minimal: tuple[int, ...] = key(
        (1700, 2000, 600, 400, 2400, 1000, 300, 1200), serves="PRM-09", layers=ENGINEERING, unit="characters"
    )
    prompt_budget_cloud_topic: int = key(16000, serves="PRM-09", layers=ENGINEERING, unit="characters", ge=100)
    prompt_budget_cloud_snapshot: int = key(4000, serves="PRM-09", layers=ENGINEERING, unit="characters", ge=100)
    prompt_team_soul_max_chars: int = key(600, serves="PRM-04", layers=ENGINEERING, unit="characters", ge=100)
    # --- M14.g and M14.h: one-shot reminders, untrusted input caps and the scanner's gates (proposals) ---
    reminder_max_chars: int = key(300, serves="PRM-16", layers=ENGINEERING, unit="characters", ge=40, le=1000)
    untrusted_input_context_share: float = key(0.06, serves="PRM-20", layers=ENGINEERING, unit="share", ge=0.01,
                                               le=0.5)  # fmt: skip
    untrusted_input_max_chars: int = key(4000, serves="PRM-20", layers=ENGINEERING, unit="characters", ge=200)
    untrusted_head_share: float = key(0.7, serves="PRM-20", layers=ENGINEERING, unit="share", ge=0.1, le=0.9)
    untrusted_tail_share: float = key(0.2, serves="PRM-20", layers=ENGINEERING, unit="share", ge=0.05, le=0.5)
    injection_scan_min_recall: float = key(0.95, serves="PRM-18", layers=ENGINEERING, unit="share", ge=0.5, le=1)
    injection_scan_max_flag_rate: float = key(0.01, serves="PRM-18", layers=ENGINEERING, unit="share", ge=0, le=0.2)
    injection_ordinary_messages: int = key(2000, serves="PRM-18", layers=ENGINEERING, unit="messages", ge=100)
    # --- M14.g tone and reading level (PED-21 caps beyond the O level one above, LOC-22 readability; proposals) ---
    coach_words_per_turn_a: int = key(90, serves="PED-21", layers=ENGINEERING, unit="words", ge=10, le=300)
    coach_words_per_turn_support: int = key(40, serves="PED-21", layers=ENGINEERING, unit="words", ge=10, le=300)
    readability_max_sentence_words: float = key(20.0, serves="LOC-22", layers=ENGINEERING, unit="words", ge=5, le=60)
    readability_max_grade_o: float = key(8.0, serves="LOC-22", layers=ENGINEERING, unit="grade", ge=1, le=20)
    readability_max_grade_a: float = key(10.0, serves="LOC-22", layers=ENGINEERING, unit="grade", ge=1, le=20)

    # --- turn engine (AGT-08, AGT-09/PED-08, AGT-11, AGT-16, AGT-28; proposals until the bake-off) ---
    gate_nudges_per_gate: int = key(2, serves="AGT-08", layers=ENGINEERING, unit="nudges", ge=0, le=4)
    gate_nudges_per_turn: int = key(4, serves="AGT-08", layers=ENGINEERING, unit="nudges", ge=0, le=4)
    leak_regenerations: int = key(2, serves="PED-08", layers=ENGINEERING, unit="regenerations", ge=0, le=4)
    leak_regenerations_low: int = key(1, serves="PED-08", layers=ENGINEERING, unit="regenerations", ge=0, le=4)
    # D128: the hardware classes that run the lean turn (`engine/lean.py`): lean decode shapes, a Coach without tools
    # or visuals, fewer Diagnoser candidates, and a reply with more than one ask repaired rather than regenerated.
    lean_classes: tuple[str, ...] = key(("low",), serves="AGT-28", layers=ENGINEERING, unit="hardware classes")
    # W3 (SPEC §7): device classes whose turns run as the low class (its short prompts, lean shapes and fewer calls)
    # while the Coach is served on the device: Muse Glimmer reads ~230 prompt tokens/s and reasons before every reply,
    # so the full turn would take minutes. The session's own class returns with the cloud. Empty: never.
    offline_lean_classes: tuple[str, ...] = key((), serves="AGT-28", layers=ENGINEERING, unit="hardware classes")
    one_ask_regenerations_lean: int = key(0, serves="PED-28", layers=ENGINEERING, unit="regenerations", ge=0, le=4)
    leak_relative_tolerance: float = key(1e-6, serves="PED-08", layers=ENGINEERING, ge=0, le=0.1)
    budget_reminder_fractions: tuple[float, ...] = key((0.5, 0.85), serves="AGT-08", layers=ENGINEERING)
    guard_continuations: int = key(2, serves="AGT-11", layers=ENGINEERING, unit="continuations", ge=0, le=5)
    guard_repetition_share: float = key(0.5, serves="AGT-11", layers=ENGINEERING, ge=0.1, le=1)
    guard_same_call_warn: int = key(2, serves="AGT-11", layers=ENGINEERING, unit="calls", ge=1, le=20)
    guard_same_call_block: int = key(5, serves="AGT-11", layers=ENGINEERING, unit="calls", ge=1, le=20)
    guard_cycle_period: int = key(4, serves="AGT-11", layers=ENGINEERING, unit="calls", ge=1, le=10)
    guard_cycle_repeats: int = key(3, serves="AGT-11", layers=ENGINEERING, unit="repeats", ge=2, le=10)
    guard_text_repeats: int = key(3, serves="AGT-11", layers=ENGINEERING, unit="replies", ge=2, le=10)
    guard_strike_turns: int = key(3, serves="AGT-11", layers=ENGINEERING, unit="replies", ge=1, le=10)
    cancel_deadline_s: float = key(1.0, serves="AGT-16", layers=ENGINEERING, unit="s", ge=0.05, le=30)
    # --- roles, delegation, steering and dedupe (AGT-02, AGT-15, AGT-17, AGT-18, AGT-27, TOL-13; proposals) ---
    tool_dedupe_entries: int = key(512, serves="TOL-13", layers=ENGINEERING, unit="entries per session", ge=0)
    steer_queue_max: int = key(3, serves="AGT-17", layers=ENGINEERING, unit="messages", ge=1, le=10)
    side_question_max_calls: int = key(2, serves="AGT-18", layers=ENGINEERING, unit="calls", ge=1, le=5)
    vision_confirm_threshold: float = key(0.6, serves="AGT-02", layers=ENGINEERING, ge=0, le=1)
    title_max_words: int = key(6, serves="AGT-02", layers=ENGINEERING, unit="words", ge=1, le=20)
    diagnoser_attempts: int = key(2, serves="AGT-27", layers=ENGINEERING, unit="attempts", ge=1, le=5)
    # A delegated role served by the device's own model has its manifest limit stretched by this (measured on Muse
    # Glimmer, 4 Nov: the Checker needs tool rounds and shares the runtime with the Diagnoser).
    local_wall_clock_scale: float = key(3.0, serves="AGT-27", layers=ENGINEERING, unit="ratio", ge=1, le=10)
    # The whole of a Your tutor turn on the device's own model; the slowest complete turn measured was 123 s.
    companion_local_turn_s: float = key(240.0, serves="AGT-27", layers=ENGINEERING, unit="seconds", ge=1, le=1800)
    # SPEC §12: on a new question served online, the Solver starts beside the Diagnoser on a guessed teaching skill.
    speculative_solver: bool = key(True, serves="AGT-15", layers=ENGINEERING)
    topic_chooser_options: int = key(3, serves="AGT-27", layers=ENGINEERING, unit="options", ge=1, le=4)
    # The KCs the Diagnoser is offered for a question, best-matching first (AGT-01, LRN-06).
    diagnoser_kc_candidates: int = key(20, serves="AGT-01", layers=ENGINEERING, unit="KCs", ge=1, le=60)
    # D128: on a lean class every candidate line is read at about 18 tokens/s, so fewer are offered; misconceptions
    # are offered only with a student answer, the only time one can be chosen.
    diagnoser_kc_candidates_lean: int = key(4, serves="AGT-01", layers=ENGINEERING, unit="KCs", ge=1, le=60)
    coach_turn_ceiling: int = key(6, serves="AGT-28", layers=ENGINEERING, unit="calls", ge=1, le=20)
    # Operational limit for a short JSON reply, or first content on a stream, separate from the 3s target. The
    # existing Coach role's 20s window remains the safe default: 3s/5s cutoffs failed real Gemini 3.8 exchanges.
    # A reviewed lower-latency model configuration may select a shorter window after measurement.
    coach_cloud_response_timeout_s: float = key(20.0, serves="AGT-28", layers=ENGINEERING, unit="s", ge=0.1, le=30)
    coach_max_iterations: tuple[int, ...] = key(
        (3, 3, 4, 6), serves="AGT-28", layers=ENGINEERING, unit="iterations (low, mid, high, cloud)"
    )
    coach_tool_rounds: int = key(2, serves="AGT-28", layers=ENGINEERING, unit="rounds", ge=0, le=12)
    coach_words_per_turn: int = key(60, serves="PED-21", layers=ENGINEERING, unit="words", ge=10, le=300)

    # --- context and compaction (MEM-15, MEM-17, MEM-20; proposals until the bake-off) ---
    compaction_threshold: float = key(0.75, serves="MEM-15", layers=ENGINEERING, ge=0.3, le=0.95)
    compaction_recent_exchanges: int = key(3, serves="MEM-15", layers=ENGINEERING, unit="exchanges", ge=1, le=20)
    compaction_recent_share: float = key(0.25, serves="MEM-15", layers=ENGINEERING, ge=0, le=1)
    compaction_section_max_chars: int = key(600, serves="MEM-17", layers=ENGINEERING, unit="characters", ge=50)
    context_budget_low: tuple[int, ...] = key(
        (1200, 600, 250, 75, 300, 2900, 200, 619, 4608), serves="MEM-20", layers=ENGINEERING, unit="tokens"
    )
    context_budget_mid: tuple[int, ...] = key(
        (1300, 1200, 400, 75, 400, 3700, 300, 817, 6144), serves="MEM-20", layers=ENGINEERING, unit="tokens"
    )
    context_budget_high: tuple[int, ...] = key(
        (2000, 2000, 600, 75, 500, 8100, 1400, 1709, 12288), serves="MEM-20", layers=ENGINEERING, unit="tokens"
    )
    context_budget_cloud: tuple[int, ...] = key(
        (2300, 4000, 1000, 75, 500, 19600, 2000, 3293, 24576), serves="MEM-20", layers=ENGINEERING, unit="tokens"
    )

    # --- pack search and the knowledge tools (CUR-30, MEM-21, TOL-21, TOL-29, EVL-13) ---
    content_search_max_results: int = key(5, serves="TOL-21", layers=ENGINEERING, unit="passages", ge=1, le=20)
    content_search_passage_max_chars: int = key(1200, serves="TOL-21", layers=ENGINEERING, unit="characters", ge=100)
    curriculum_lookup_max_candidates: int = key(8, serves="TOL-29", layers=ENGINEERING, unit="records", ge=1, le=40)
    curriculum_lookup_max_suggestions: int = key(3, serves="TOL-29", layers=ENGINEERING, unit="ids", ge=1, le=10)
    # Engineering: how many BM25 and vector candidates each ranking contributes before reciprocal-rank fusion, and the
    # fusion constant.
    retrieval_candidate_pool: int = key(50, serves="MEM-21", layers=ENGINEERING, unit="passages", ge=5, le=1000)
    retrieval_rrf_k: int = key(60, serves="MEM-21", layers=ENGINEERING, ge=1, le=1000)
    # Engineering: how many candidate topics retrieval gives the Diagnoser to choose from.
    classify_topic_candidates: int = key(8, serves="EVL-13", layers=ENGINEERING, unit="topics", ge=1, le=40)

    # --- skills, visuals and diagnostics (SKL-07, VSL-09, VSL-12, VSL-13, OBS-07, OBS-11) ---
    skill_index_max_lines: int = key(25, serves="SKL-07", layers=ENGINEERING, unit="lines", ge=1, le=200)
    skill_index_max_chars: int = key(2000, serves="SKL-07", layers=ENGINEERING, unit="characters", ge=100)
    skill_push_max_chars: int = key(4000, serves="SKL-08", layers=ENGINEERING, unit="characters", ge=200)
    # SKL-21 colocated eval minimums by kind (teaching: one per misconception and at least this many) and the hard
    # assertion pass rate a reviewed skill keeps.
    skill_eval_min_teaching: int = key(3, serves="SKL-21", layers=ENGINEERING, unit="cases", ge=1, le=50)
    skill_eval_min_pedagogy: int = key(5, serves="SKL-21", layers=ENGINEERING, unit="cases", ge=1, le=50)
    skill_eval_min_agent: int = key(5, serves="SKL-21", layers=ENGINEERING, unit="cases", ge=1, le=50)
    skill_eval_hard_pass_rate: float = key(1.0, serves="SKL-21", layers=ENGINEERING, unit="share", ge=0.5, le=1)
    visual_request_max_kb: int = key(16, serves="VSL-09", layers=ENGINEERING, unit="KB", ge=1, le=1024)
    visual_resolved_max_kb: int = key(256, serves="VSL-09", layers=ENGINEERING, unit="KB", ge=1, le=8192)
    visual_relative_tolerance: float = key(1e-9, serves="VSL-12", layers=ENGINEERING, ge=0, le=1e-3)
    visual_resolution_budget_ms: int = key(300, serves="VSL-13", layers=ENGINEERING, unit="ms", ge=10, le=60000)

    # --- the board performance layer's motion (DSN-10, D103; S3: UXS-55 write-on, UXS-56 pen, UXS-57 spotlight) ---
    board_write_on_char_ms: int = key(40, serves="DSN-10", layers=ENGINEERING, unit="ms", ge=0, le=500, client=True)
    board_write_on_line_max_ms: int = key(1200, serves="DSN-10", layers=ENGINEERING, unit="ms", ge=0, le=10000,
                                          client=True)  # fmt: skip
    board_pen_travel_ms: int = key(300, serves="DSN-10", layers=ENGINEERING, unit="ms", ge=0, le=2000, client=True)
    board_spotlight_dim_opacity: float = key(0.4, serves="DSN-10", layers=ENGINEERING, unit="opacity", ge=0, le=1,
                                             client=True)  # fmt: skip
    # VSL-43, VSL-44, MDL-33 (D106, D107): online sketches. A sketch ready within the deadline joins the turn; a later
    # one is added after it. The caps and the checks' tolerances (a share of the frame's diagonal, or degrees).
    # EVL-28 (D106): the sketch kinds whose correctness gate the suite passed; a kind not listed keeps sketch calls off.
    # Empty until the release's suite report turns kinds on, so sketches go live only through M44's exit.
    sketch_live_kinds: tuple[str, ...] = key((), serves="EVL-28", layers=ENGINEERING, unit="sketch kinds")
    sketch_deadline_ms: int = key(3000, serves="VSL-43", layers=ENGINEERING, unit="ms", ge=500, le=20000)
    sketch_max_elements: int = key(40, serves="VSL-43", layers=ENGINEERING, unit="elements", ge=1, le=40)
    sketch_max_labels: int = key(12, serves="VSL-43", layers=ENGINEERING, unit="labels", ge=0, le=40)
    sketch_max_completion_tokens: int = key(600, serves="MDL-33", layers=ENGINEERING, unit="tokens", ge=100, le=2000)
    sketch_position_share: float = key(0.02, serves="VSL-44", layers=ENGINEERING, unit="share", ge=0.001, le=0.2)
    sketch_length_share: float = key(0.03, serves="VSL-44", layers=ENGINEERING, unit="share", ge=0.001, le=0.2)
    sketch_angle_deg: float = key(3.0, serves="VSL-44", layers=ENGINEERING, unit="degrees", ge=0.1, le=20)
    # DSN-27: the speaking mouth from the loudness of the audio played (level 0 at -60 dBFS to 1 at 0 dBFS per 20 ms
    # frame): the levels where the small, medium and open shapes start, the hysteresis below each, and the smoothing.
    presence_mouth_small_level: float = key(0.45, serves="DSN-27", layers=ENGINEERING, unit="level", ge=0, le=1,
                                            client=True)  # fmt: skip
    presence_mouth_medium_level: float = key(0.6, serves="DSN-27", layers=ENGINEERING, unit="level", ge=0, le=1,
                                             client=True)  # fmt: skip
    presence_mouth_open_level: float = key(0.75, serves="DSN-27", layers=ENGINEERING, unit="level", ge=0, le=1,
                                           client=True)  # fmt: skip
    presence_mouth_hysteresis: float = key(0.05, serves="DSN-27", layers=ENGINEERING, unit="level", ge=0, le=0.2,
                                           client=True)  # fmt: skip
    presence_mouth_attack_ms: int = key(30, serves="DSN-27", layers=ENGINEERING, unit="ms", ge=0, le=500, client=True)
    presence_mouth_release_ms: int = key(90, serves="DSN-27", layers=ENGINEERING, unit="ms", ge=0, le=500, client=True)

    # --- visual catalogue, diagram library and simulations (VSL-20 to VSL-33) ---
    visual_step_sync_ms: int = key(100, serves="VSL-20", layers=ENGINEERING, unit="ms", ge=16, le=1000, client=True)
    visual_step_transition_ms: int = key(200, serves="VSL-20", layers=ENGINEERING, unit="ms", ge=0, le=1000,
                                         client=True)  # fmt: skip
    visual_focus_ttl_ms: int = key(3000, serves="VSL-21", layers=ENGINEERING, unit="ms", ge=500, le=30000, client=True)
    # M43.f (D103): `serves` below names requirements of the pinned specification; VOC-51, VOC-52, UXS-63 and VSL-45
    # join them when the monorepo is re-pinned to D103.
    # VOC-10, VSL-45: the visuals of the current problem a spoken cue may point at (the most recent ones).
    pointer_visuals_kept: int = key(4, serves="VOC-10, VSL-21", layers=ENGINEERING, unit="visuals", ge=1, le=12)
    # VOC-10, UXS-63, DSN-10: the temporary pointer ring clears after this long, or at the next pointer.
    pointer_ttl_ms: int = key(3000, serves="VOC-10, VSL-21", layers=ENGINEERING, unit="ms", ge=500, le=30000,
                              client=True)  # fmt: skip
    # VOC-51: how often the client reads the audio clock while cues are pending (well inside the 100 ms budget).
    cue_tick_ms: int = key(25, serves="VOC-10", layers=ENGINEERING, unit="ms", ge=5, le=100, client=True)
    # VOC-51 acceptance: a cue fires within this long of the moment its word is heard.
    cue_fire_tolerance_ms: int = key(100, serves="VOC-10", layers=ENGINEERING, unit="ms", ge=10, le=500, client=True)
    # VOC-52, VOC-36: a hint or demonstration counts only once this share of its teaching unit has played, so a
    # barge-in on its first words does not advance learner state.
    heard_unit_share: float = key(1.0, serves="VOC-36", layers=ENGINEERING, unit="share", ge=0.5, le=1)
    visual_raster_max_kb: int = key(400, serves="VSL-23", layers=ENGINEERING, unit="KB", ge=1, le=4096)
    sim_steps_per_s: int = key(60, serves="VSL-30", layers=ENGINEERING, unit="steps/s", ge=10, le=240, client=True)
    sim_titration_rate_cm3_s: float = key(0.5, serves="VSL-30", layers=ENGINEERING, unit="cm3/s", ge=0.01, le=10,
                                          client=True)  # fmt: skip
    sim_readout_announce_ms: int = key(1000, serves="VSL-32", layers=ENGINEERING, unit="ms", ge=250, le=10000,
                                       client=True)  # fmt: skip
    visual_first_render_ms: int = key(200, serves="VSL-33", layers=ENGINEERING, unit="ms", ge=10, le=5000, client=True)
    visual_step_paint_ms: int = key(100, serves="VSL-33", layers=ENGINEERING, unit="ms", ge=10, le=2000, client=True)
    visual_library_svg_max_kb: int = key(
        300, serves="VSL-33", layers=ENGINEERING, unit="KB", ge=1, le=4096, client=True
    )
    visual_library_max_parts: int = key(200, serves="VSL-33", layers=ENGINEERING, unit="parts", ge=1, le=2000,
                                        client=True)  # fmt: skip
    visual_template_max_parts: int = key(300, serves="VSL-33", layers=ENGINEERING, unit="parts", ge=1, le=2000,
                                         client=True)  # fmt: skip
    visual_curve_max_points: int = key(2000, serves="VSL-33", layers=ENGINEERING, unit="points", ge=50, le=20000,
                                       client=True)  # fmt: skip
    visual_curve_max_count: int = key(4, serves="VSL-33", layers=ENGINEERING, unit="curves", ge=1, le=12, client=True)
    sim_min_fps: int = key(30, serves="VSL-33", layers=ENGINEERING, unit="fps", ge=10, le=120, client=True)
    sim_target_fps: int = key(60, serves="VSL-33", layers=ENGINEERING, unit="fps", ge=10, le=240, client=True)
    sim_fidelity_sf: int = key(3, serves="VSL-31", layers=ENGINEERING, unit="significant figures", ge=1, le=10,
                               client=True)  # fmt: skip
    support_mode_hours: float = key(24.0, serves="OBS-07", layers=ENGINEERING, unit="h", ge=1, le=168)
    log_rotation_files: int = key(5, serves="OBS-11", layers=ENGINEERING, unit="files", ge=1, le=50)
    log_rotation_mb: int = key(5, serves="OBS-11", layers=ENGINEERING, unit="MB", ge=1, le=500)

    # --- model router (MDL-08; MDL-16 and MDL-17 subsets) ---
    router_retry_same_model: int = key(2, serves="MDL-08", layers=ENGINEERING, unit="retries", ge=0, le=5)
    router_backoff_initial_s: float = key(2.0, serves="MDL-08", layers=ENGINEERING, unit="s", ge=0.01, le=60)
    router_backoff_factor: float = key(2.0, serves="MDL-08", layers=ENGINEERING, ge=1, le=10)
    router_backoff_max_s: float = key(60.0, serves="MDL-08", layers=ENGINEERING, unit="s", ge=0.01, le=600)
    router_backoff_jitter: float = key(0.25, serves="MDL-08", layers=ENGINEERING, ge=0, le=1)
    context_cap_low: int = key(6144, serves="MDL-16", layers=ENGINEERING, unit="tokens", ge=2048)
    context_cap_mid: int = key(8192, serves="MDL-16", layers=ENGINEERING, unit="tokens", ge=2048)
    context_cap_high: int = key(16384, serves="MDL-16", layers=ENGINEERING, unit="tokens", ge=2048)
    runtime_overhead_gb: float = key(1.5, serves="MDL-17", layers=ENGINEERING, unit="GB", ge=0, le=16)
    # D128: the runtime keeps each pinned role's prompt prefix in its own slot of one shared (unified) KV pool, with
    # llama-server's host prompt cache off so an erase leaves no profile's tokens anywhere (DAT-14). The pool is the
    # agent context plus room for the pinned prefixes. The caches stay f16: on the low reference CPU 8-bit K and V
    # halved prompt reading at a 2,500-token depth (21.6 to 9.6 tokens/s, jana-hw-low, 2026-10-21).
    runtime_cache_slots: int = key(4, serves="MDL-22", layers=ENGINEERING, unit="slots", ge=1, le=16)
    runtime_prefix_pool_tokens: int = key(2048, serves="MDL-22", layers=ENGINEERING, unit="tokens", ge=0, le=65536)
    runtime_kv_cache_type: str = key("f16", serves="MDL-17", layers=ENGINEERING)
    # D128: the classes whose runtime reads every foreground role's prompt prefix in the background once it is ready
    # (on mid and high a cold prefix costs a few seconds; on low a minute or more).
    runtime_warmup_classes: tuple[str, ...] = key(
        ("low",), serves="ARC-22", layers=ENGINEERING, unit="hardware classes"
    )
    # MDL-17: side features' resident memory added to the estimate when enabled for the profile. The photo reader's
    # is TABLE INP-29a's extra memory while reading (low, mid, high). The speech pack's is the peak measured in the
    # D95 voice bake-off on 2026-10-16: Kokoro-82M on torch CPU peaks at 2.2-2.6 GB (low, mid); Qwen3-TTS 0.6B on MLX
    # streams at 2.6 GB resident plus 2.9 GB of MLX memory (high, Apple silicon). It bounds voice tutoring (VOC-03).
    photo_reader_resident_gb: tuple[float, ...] = key(
        (1.0, 1.2, 1.5), serves="MDL-17", layers=ENGINEERING, unit="GB (low, mid, high)"
    )
    speech_pack_resident_gb: tuple[float, ...] = key(
        (2.6, 2.6, 5.6), serves="MDL-17", layers=ENGINEERING, unit="GB (low, mid, high)"
    )
    # VOC-18 (D95): read-aloud's first spoken unit ends at the first clause of the first sentence once this many words
    # precede it, because time to first audio grows with that unit's length on a slow laptop; 0 keeps whole sentences.
    read_aloud_first_clause_words: int = key(4, serves="VOC-18", layers=ENGINEERING, unit="words", ge=0, le=40)
    # TABLE VOC-03a (D95): Qwen3-TTS on MLX streams audio in chunks of this many seconds (the bake-off's 0.32 s gave
    # 0.09 s warm time to first audio).
    voice_qwen3_stream_interval_s: float = key(0.32, serves="VOC-13", layers=ENGINEERING, unit="s", ge=0.08, le=2)
    # VOC-03: the voice stack plus the tutor model keep peak memory at least this far below installed RAM; where they
    # do not, voice tutoring is unavailable and read-aloud stays.
    voice_memory_headroom_gb: float = key(2.0, serves="VOC-03", layers=ENGINEERING, unit="GB", ge=0, le=64)
    # TABLE VOC-05a and VOC-06: the run-time silence that soft-ends a turn, its thinking extension while the tutor
    # awaits worked input, and the hold-to-think value that silence cannot end.
    voice_silence_ms: int = key(160, serves="VOC-05", layers=ENGINEERING, unit="ms", ge=32, le=2000)
    voice_thinking_extension_ms: int = key(1200, serves="VOC-05", layers=ENGINEERING, unit="ms", ge=160, le=10000)
    voice_hold_to_think_ms: int = key(10000, serves="VOC-06", layers=ENGINEERING, unit="ms", ge=1000, le=60000)
    # VOC-12: with no accepted spoken form released this long after the adapter's request, one honest filler plays.
    voice_filler_after_ms: int = key(1500, serves="VOC-12", layers=ENGINEERING, unit="ms", ge=200, le=20000)
    # On cloud voice the request reaches the adapter only after the gateway ends the utterance, about 1.2 s after the
    # student stops (Voice Transcribe's end of speech and the gateway's hold, SPEC §12), so its filler waits less: it
    # still plays about 1.5 s after the speech.
    voice_cloud_filler_after_ms: int = key(300, serves="VOC-12", layers=ENGINEERING, unit="ms", ge=200, le=20000)
    # PRM-26, VOC-07, VOC-37: the spoken form of a voice turn, at most this many characters and sentences before the
    # student's next turn.
    spoken_max_chars: int = key(600, serves="PRM-26, VOC-07", layers=ENGINEERING, unit="characters", ge=50, le=4000)
    spoken_max_sentences: int = key(3, serves="PRM-26, VOC-37", layers=ENGINEERING, unit="sentences", ge=1, le=10)
    # INP-39 acceptance: the least share of the spoken-maths golden set the grammar must convert to the expected text.
    spoken_maths_golden_min_share: float = key(0.9, serves="INP-39", layers=ENGINEERING, unit="share", ge=0, le=1)
    # MDL-17 acceptance: the estimate within this share of measured resident memory on the reference laptops.
    memory_estimate_tolerance: float = key(0.15, serves="MDL-17", layers=ENGINEERING, unit="share", ge=0.01, le=1)
    # Thinking per hardware class (MDL-12): 0 is off; router.yaml local.thinking_budget mirrors it.
    # Off on every class for local models: with thinking on, Gemma 4 12B's Solver replies were 92% well formed at the
    # high class, and 100% with it off at no loss of accuracy (D6 condition, implementation/bakeoff-high-2026-10-12).
    router_thinking_budget: tuple[int, ...] = key(
        (0, 0, 0), serves="MDL-12", layers=ENGINEERING, unit="tokens (low, mid, high)"
    )
    router_effort_budget: tuple[int, ...] = key(
        (0, 512, 2048, 8192), serves="MDL-12", layers=ENGINEERING, unit="tokens (none, low, medium, high)"
    )
    # Local queue (MDL-22); the per-role limits are mirrored by the roles' `concurrency` in router.yaml.
    router_concurrency_coach: int = key(1, serves="MDL-22", layers=ENGINEERING, unit="calls", ge=1, le=8)
    router_concurrency_checker: int = key(4, serves="MDL-22", layers=ENGINEERING, unit="calls", ge=1, le=8)
    router_concurrency_reflector: int = key(1, serves="MDL-22", layers=ENGINEERING, unit="calls", ge=1, le=8)
    router_preempt_start_ms: int = key(500, serves="MDL-22", layers=ENGINEERING, unit="ms", ge=10, le=10000)

    # --- the minimum laptop and the hardware class entry rules (PLT-29, TABLE PLT-08a; proposals until measured) ---
    minimum_ram_gb: float = key(8.0, serves="PLT-29", layers=ENGINEERING, unit="GB", ge=1, le=256,
                                desktop_shell=True)  # fmt: skip
    minimum_free_disk_gb: float = key(10.0, serves="PLT-29", layers=ENGINEERING, unit="GB", ge=1, le=1000,
                                      desktop_shell=True)  # fmt: skip
    minimum_cpu_year: int = key(2018, serves="PLT-29", layers=ENGINEERING, unit="year", ge=2000, le=2100)
    ram_report_slack_gb: float = key(0.5, serves="PLT-29", layers=ENGINEERING, unit="GB", ge=0, le=2)
    # D128: the low class's speeds, measured after the benchmark's realistic prompt (`probe.BENCHMARK_PROMPT`): the
    # speeds at which a first turn (about 1,460 tokens read, 400 written) and a guided step (500, 80) meet EVL-14.
    low_class_min_tokens_s: float = key(8.0, serves="PLT-08", layers=ENGINEERING, unit="tokens/s", ge=0.1)
    low_class_min_prompt_tokens_s: float = key(40.0, serves="PLT-08", layers=ENGINEERING, unit="tokens/s", ge=0.1)
    mid_class_min_ram_gb: float = key(14.0, serves="PLT-08", layers=ENGINEERING, unit="GB", ge=1, le=256)
    mid_class_min_tokens_s: float = key(10.0, serves="PLT-08", layers=ENGINEERING, unit="tokens/s", ge=0.1)
    high_class_min_tokens_s: float = key(25.0, serves="PLT-08", layers=ENGINEERING, unit="tokens/s", ge=0.1)
    high_class_min_memory_gb: float = key(16.0, serves="PLT-08", layers=ENGINEERING, unit="GB", ge=1, le=1024)
    benchmark_max_s: float = key(90.0, serves="PLT-08", layers=ENGINEERING, unit="s", ge=1, le=600)
    # PLT-08 acceptance: the benchmark repeated on a reference laptop picks its class every time.
    benchmark_reference_runs: int = key(10, serves="PLT-08", layers=ENGINEERING, unit="runs", ge=1, le=100)
    # PLT-09: suggest a lower class when more than this share of the window's guided steps is over budget.
    class_suggest_lower_share: float = key(0.2, serves="PLT-09", layers=ENGINEERING, unit="share", ge=0, le=1)
    # PLT-04: a Vulkan device qualifies with this much dedicated memory, or this much shared memory (integrated);
    # the shell's device probe of the Vulkan build may take this long before it counts as failed.
    runtime_vulkan_min_dedicated_gb: float = key(
        2.0, serves="PLT-04", layers=ENGINEERING, unit="GB", ge=0.5, le=64, desktop_shell=True
    )
    runtime_vulkan_min_shared_gb: float = key(
        4.0, serves="PLT-04", layers=ENGINEERING, unit="GB", ge=0.5, le=128, desktop_shell=True
    )
    runtime_variant_probe_s: float = key(
        10.0, serves="PLT-04", layers=ENGINEERING, unit="s", ge=0.5, le=120, desktop_shell=True
    )

    tool_max_result_chars_small: int = key(8000, serves="TOL-12", layers=ENGINEERING, unit="characters", ge=500)
    tool_max_result_chars_full: int = key(50000, serves="TOL-12", layers=ENGINEERING, unit="characters", ge=500)
    tool_max_data_kb: int = key(256, serves="TOL-12", layers=ENGINEERING, unit="KB", ge=1)
    tool_error_max_chars: int = key(2000, serves="TOL-10", layers=ENGINEERING, unit="characters", ge=100)
    tool_name_match_min: float = key(0.7, serves="TOL-08", layers=ENGINEERING, ge=0.3, le=1)
    tool_name_match_margin: float = key(0.05, serves="TOL-08", layers=ENGINEERING, ge=0, le=0.5)
    tool_availability_grace_s: float = key(30.0, serves="TOL-06", layers=ENGINEERING, unit="s", ge=0, le=600)
    tool_description_max_full: int = key(600, serves="TOL-07", layers=ENGINEERING, unit="characters", ge=100)
    tool_description_max_small: int = key(280, serves="TOL-07", layers=ENGINEERING, unit="characters", ge=80)
    tool_small_max_properties: int = key(6, serves="TOL-07", layers=ENGINEERING, unit="properties", ge=1, le=20)
    tool_small_max_enum: int = key(12, serves="TOL-07", layers=ENGINEERING, unit="values", ge=2, le=50)
    tool_small_max_tools: int = key(8, serves="TOL-17", layers=ENGINEERING, unit="tools", ge=1, le=32)
    board_ack_deadline_s: float = key(3.0, serves="TOL-18", layers=ENGINEERING, unit="s", ge=0.1, le=60)

    release_slsa_target_level: int = key(3, serves="SEC-22", layers=(Layer.DEFAULTS,), unit="level", ge=1, le=4)

    # --- local API and event stream (API-16 to API-24, API-31; proposals) ---
    api_ring_events: int = key(512, serves="API-16", layers=ENGINEERING, unit="events", ge=16, le=100_000)
    api_ring_mb: float = key(4.0, serves="API-16", layers=ENGINEERING, unit="MB", ge=0.1, le=256)
    api_ring_process_mb: float = key(64.0, serves="API-16", layers=ENGINEERING, unit="MB", ge=1, le=4096)
    api_ring_sessions: int = key(64, serves="API-16", layers=ENGINEERING, unit="sessions", ge=1, le=10_000)
    api_ring_event_kb: float = key(64.0, serves="API-16", layers=ENGINEERING, unit="KB", ge=1, le=16_384)
    api_flush_ms: int = key(33, serves="API-18", layers=ENGINEERING, unit="ms", ge=30, le=50)
    api_flush_kb: float = key(4.0, serves="API-18", layers=ENGINEERING, unit="KB", ge=0.5, le=1024)
    api_heartbeat_s: float = key(15.0, serves="API-19", layers=ENGINEERING, unit="s", ge=0.05, le=300)
    api_client_dead_s: float = key(45.0, serves="API-19", layers=ENGINEERING, unit="s", ge=0.15, le=900)
    api_queue_events: int = key(2000, serves="API-21", layers=ENGINEERING, unit="events", ge=8, le=1_000_000)
    api_queue_mb: float = key(8.0, serves="API-21", layers=ENGINEERING, unit="MB", ge=0.1, le=1024)
    api_cancel_budget_ms: int = key(500, serves="API-24", layers=ENGINEERING, unit="ms", ge=50, le=10_000)
    api_idempotency_hours: float = key(24.0, serves="API-31", layers=ENGINEERING, unit="h", ge=0.01, le=720)
    shutdown_grace_s: float = key(3.0, serves="ARC-23", layers=ENGINEERING, unit="s", ge=0.1, le=60, desktop_shell=True)
    shutdown_total_s: float = key(
        10.0, serves="ARC-23", layers=ENGINEERING, unit="s", ge=0.5, le=120, desktop_shell=True
    )

    # --- shell supervision (ARC-24; the runtime's own policy is MDL-19, the worker's TOL-26; proposals) ---
    core_restart_backoff_s: tuple[float, ...] = key(
        (0.5, 2.0, 8.0), serves="ARC-24", layers=ENGINEERING, unit="s", desktop_shell=True
    )
    core_restart_max: int = key(3, serves="ARC-24", layers=ENGINEERING, unit="crashes", ge=1, le=20, desktop_shell=True)
    core_restart_window_s: float = key(
        120.0, serves="ARC-24", layers=ENGINEERING, unit="s", ge=1, le=3600, desktop_shell=True
    )
    # The runtime's restart policy itself is the router configuration's `local.restart` (MDL-19), carried to the
    # shell in the launch plan. These are the waits around it (engineering values, not from the specification).
    runtime_start_deadline_s: float = key(
        30.0, serves="MDL-19", layers=ENGINEERING, unit="s", ge=0.5, le=600, desktop_shell=True
    )
    runtime_liveness_interval_s: float = key(2.0, serves="MDL-19", layers=ENGINEERING, unit="s", ge=0.05, le=60)
    # The model warms in the background within this time of launch while the home screen is usable (MDL-20); a
    # device-lab budget like startup_home_ready_budget_s.
    runtime_warm_budget_s: float = key(60.0, serves="MDL-20", layers=ENGINEERING, unit="s", ge=5, le=600)
    # A published security fix in the runtime's model loader or server is taken within this many days (SEC-24); the
    # release review checks the pinned llama.cpp commit against it.
    runtime_security_fix_days: int = key(14, serves="SEC-24", layers=ENGINEERING, unit="days", ge=1, le=90)
    runtime_restart_wait_s: float = key(60.0, serves="MDL-19", layers=ENGINEERING, unit="s", ge=0.1, le=600)
    # ARC-31: the runtime yields to the app. Both reach the shell in the launch plan, like the restart policy. Its
    # thread cap is the machine's physical cores minus `runtime_reserved_cores` (at least one thread); its niceness
    # is the Unix value, and Windows uses the below-normal priority class instead. The memory headroom of the same
    # requirement is the router configuration's `local.memory_headroom_gb`.
    runtime_reserved_cores: int = key(1, serves="ARC-31", layers=ENGINEERING, unit="physical cores", ge=0, le=64)
    runtime_niceness: int = key(10, serves="ARC-31", layers=ENGINEERING, unit="nice", ge=1, le=19)

    # --- health panel (OBS-15): the window its medians cover, and the fewest turns a median needs before a slow
    # computer is named (an engineering value, not from the specification) ---
    health_window_days: int = key(7, serves="OBS-15", layers=ENGINEERING, unit="days", ge=1, le=30)
    health_min_turns: int = key(5, serves="OBS-15", layers=ENGINEERING, unit="turns", ge=1, le=1000)

    # --- client (UI) timing and rendering (ARC-02, API-20, UIA-03, UIA-07, UIA-11, UIA-15, UIA-16, UXS-03) ---
    ui_input_paint_ms: int = key(100, serves="ARC-02", layers=ENGINEERING, unit="ms", ge=16, le=1000, client=True)
    ui_reconnect_base_ms: int = key(250, serves="API-20", layers=ENGINEERING, unit="ms", ge=10, le=10_000,
                                    client=True)  # fmt: skip
    ui_reconnect_max_ms: int = key(5000, serves="API-20", layers=ENGINEERING, unit="ms", ge=100, le=120_000,
                                   client=True)  # fmt: skip
    ui_reconnect_reset_ms: int = key(5000, serves="API-20", layers=ENGINEERING, unit="ms", ge=100, le=120_000,
                                     client=True)  # fmt: skip
    ui_stream_dead_ms: int = key(45_000, serves="API-19", layers=ENGINEERING, unit="ms", ge=150, le=900_000,
                                 client=True)  # fmt: skip
    ui_flush_floor_ms: int = key(33, serves="UIA-03", layers=ENGINEERING, unit="ms", ge=4, le=1000, client=True)
    ui_flush_slow_commit_ms: int = key(12, serves="UIA-03", layers=ENGINEERING, unit="ms", ge=1, le=1000,
                                       client=True)  # fmt: skip
    ui_flush_ceiling_ms: int = key(250, serves="UIA-03", layers=ENGINEERING, unit="ms", ge=16, le=5000, client=True)
    ui_flush_fast_commits: int = key(10, serves="UIA-03", layers=ENGINEERING, unit="commits", ge=1, le=1000,
                                     client=True)  # fmt: skip
    ui_banner_after_ms: int = key(2000, serves="UIA-07", layers=ENGINEERING, unit="ms", ge=0, le=60_000, client=True)
    ui_running_label_after_ms: int = key(200, serves="UIA-11", layers=ENGINEERING, unit="ms", ge=0, le=5000,
                                         client=True)  # fmt: skip
    ui_katex_cache_entries: int = key(2000, serves="UIA-15", layers=ENGINEERING, unit="entries", ge=16,
                                      le=100_000, client=True)  # fmt: skip
    ui_katex_max_expand: int = key(1000, serves="UIA-15", layers=ENGINEERING, unit="expansions", ge=10,
                                   le=100_000, client=True)  # fmt: skip
    ui_katex_max_size_em: float = key(20.0, serves="UIA-15", layers=ENGINEERING, unit="em", ge=1, le=200,
                                      client=True)  # fmt: skip
    ui_stick_bottom_px: int = key(48, serves="UIA-16", layers=ENGINEERING, unit="px", ge=0, le=1000, client=True)
    ui_progress_label_after_ms: int = key(200, serves="UXS-03", layers=ENGINEERING, unit="ms", ge=0, le=5000,
                                          client=True)  # fmt: skip
    ui_first_token_wait_label_ms: int = key(3000, serves="UXS-03", layers=ENGINEERING, unit="ms", ge=0,
                                            le=60_000, client=True)  # fmt: skip
    ui_slow_strong_tier_ms: int = key(10_000, serves="UXS-03", layers=ENGINEERING, unit="ms", ge=0, le=600_000,
                                      client=True)  # fmt: skip
    ui_still_working_ms: int = key(30_000, serves="UXS-03", layers=ENGINEERING, unit="ms", ge=0, le=600_000,
                                   client=True)  # fmt: skip
    ui_no_reply_ms: int = key(120_000, serves="UXS-03", layers=ENGINEERING, unit="ms", ge=0, le=3_600_000,
                              client=True)  # fmt: skip
    # UXS-22: a mastery change shows on the subject map's tile within this long (LRN-34's bar budget).
    ui_mastery_refresh_ms: int = key(1000, serves="UXS-22", layers=ENGINEERING, unit="ms", ge=50, le=10_000,
                                     client=True)  # fmt: skip
    # UXS-01: how many recent sessions the sidebar lists, grouped by day; UXS-12: how many sessions Home offers to pick
    # up and how many due ideas it suggests for today's practice.
    ui_recent_sessions_max: int = key(20, serves="UXS-01", layers=ENGINEERING, unit="sessions", ge=1, le=200,
                                      client=True)  # fmt: skip
    ui_home_pickup_max: int = key(2, serves="UXS-12", layers=ENGINEERING, unit="sessions", ge=1, le=10, client=True)
    ui_home_practice_max: int = key(3, serves="UXS-12", layers=ENGINEERING, unit="ideas", ge=1, le=20, client=True)
    # UXS-12: Home (with its plan) is usable within this long of launch, while the model loads in the background; while
    # it loads, the core's health is read again this often, so a question typed meanwhile goes once the tutor is ready.
    ui_home_usable_ms: int = key(5000, serves="UXS-12", layers=ENGINEERING, unit="ms", ge=500, le=60_000,
                                 client=True)  # fmt: skip
    ui_health_poll_ms: int = key(1500, serves="UXS-12", layers=ENGINEERING, unit="ms", ge=250, le=60_000,
                                 client=True)  # fmt: skip
    # UXS-51: the worked-solution search lists at most this many matches, each previewed by its stem's first line cut
    # to this many characters.
    ui_search_results_max: int = key(20, serves="UXS-51", layers=ENGINEERING, unit="matches", ge=1, le=100, client=True)
    ui_search_preview_chars: int = key(120, serves="UXS-51", layers=ENGINEERING, unit="characters", ge=20, le=400,
                                       client=True)  # fmt: skip
    # DSN-12: how long a toast (a short confirmation such as "support can see this session") stays on screen.
    ui_toast_ms: int = key(5000, serves="DSN-12", layers=ENGINEERING, unit="ms", ge=1000, le=30000, client=True)
    # UXS-24: how many questions a practice set the student starts holds (CreatePracticeSet.size, 1 to 40).
    ui_practice_set_size: int = key(5, serves="UXS-24", layers=ENGINEERING, unit="questions", ge=1, le=40, client=True)
    # UXS-12: Home's greeting says afternoon from this local hour and evening from the next.
    ui_greeting_afternoon_hour: int = key(12, serves="UXS-12", layers=ENGINEERING, unit="h", ge=0, le=23, client=True)
    ui_greeting_evening_hour: int = key(18, serves="UXS-12", layers=ENGINEERING, unit="h", ge=0, le=23, client=True)
    # --- retention (DAT-08, TABLE DAT-08a; fixed periods, never user settings, D53) ---
    # Also used here: api_idempotency_hours (API-31), trace_retention_days and trace_store_max_bytes (OBS-08),
    # crash_reports_queued_max (DAT-01, OBS-17).
    retention_job_interval_h: float = key(6.0, serves="DAT-08", layers=ENGINEERING, unit="h", ge=0.1, le=168)
    retention_assessment_run_days: int = key(90, serves="DAT-08", layers=ENGINEERING, unit="days", ge=1, le=3650)
    retention_saved_content_days: int = key(90, serves="DAT-08", layers=ENGINEERING, unit="days", ge=1, le=3650)
    retention_archive_authorization_s: float = key(300.0, serves="DAT-08", layers=ENGINEERING, unit="s", ge=10,
                                                   le=3600)  # fmt: skip
    retention_transcript_days: int = key(90, serves="DAT-08", layers=ENGINEERING, unit="days", ge=1, le=3650)
    retention_attachment_days: int = key(30, serves="DAT-08", layers=ENGINEERING, unit="days", ge=1, le=3650)
    retention_checkpoints_per_session: int = key(3, serves="DAT-08", layers=ENGINEERING, unit="checkpoints", ge=1,
                                                 le=100)  # fmt: skip
    retention_checkpoint_days: int = key(7, serves="DAT-08", layers=ENGINEERING, unit="days", ge=1, le=365)
    retention_spill_max_h: float = key(24.0, serves="DAT-08", layers=ENGINEERING, unit="h", ge=1, le=168)
    retention_note_days: int = key(180, serves="DAT-08", layers=ENGINEERING, unit="days", ge=1, le=3650)
    # SPEC §19.3.4 (W8): a resolved check-in (answered, dismissed, expired or detached) is kept this long.
    retention_commitment_days: int = key(30, serves="DAT-08", layers=ENGINEERING, unit="days", ge=1, le=3650)
    retention_digest_weeks: int = key(26, serves="DAT-08", layers=ENGINEERING, unit="weeks", ge=1, le=520)
    retention_alert_days: int = key(90, serves="DAT-08", layers=ENGINEERING, unit="days", ge=1, le=3650)
    retention_audit_days: int = key(400, serves="DAT-08", layers=ENGINEERING, unit="days", ge=1, le=3650)
    # The egress ledger's window (ARC-10); TABLE DAT-08a keeps the ledger for the same period.
    retention_egress_days: int = key(90, serves="ARC-10", layers=ENGINEERING, unit="days", ge=1, le=3650)
    retention_tombstone_days: int = key(365, serves="DAT-08", layers=ENGINEERING, unit="days", ge=1, le=3650)
    retention_telemetry_days: int = key(14, serves="DAT-08", layers=ENGINEERING, unit="days", ge=1, le=365)
    retention_device_audit_days: int = key(400, serves="DAT-08", layers=ENGINEERING, unit="days", ge=1, le=3650)
    retention_withdrawn_consent_days: int = key(400, serves="DAT-08", layers=ENGINEERING, unit="days", ge=1,
                                                le=3650)  # fmt: skip
    # --- rate limits, deletion and storage budgets (API-33, DAT-09, DAT-20; proposals) ---
    api_command_burst_per_s: float = key(20.0, serves="API-33", layers=ENGINEERING, unit="commands/s", ge=1, le=1000)
    api_turn_starts_per_min: float = key(30.0, serves="API-33", layers=ENGINEERING, unit="turns/min", ge=1, le=600)
    profile_delete_budget_s: float = key(5.0, serves="DAT-09", layers=ENGINEERING, unit="s", ge=0.5, le=120)
    storage_learner_db_budget_mb: float = key(50.0, serves="DAT-20", layers=ENGINEERING, unit="MB", ge=1)
    storage_attachments_budget_mb: float = key(200.0, serves="DAT-20", layers=ENGINEERING, unit="MB", ge=1)
    storage_spill_session_budget_mb: float = key(20.0, serves="DAT-20", layers=ENGINEERING, unit="MB", ge=1)

    # --- photos (SEC-05; proposals): decode caps and the downscaled JPEG kept and sent to a model ---
    photo_max_mb: float = key(20.0, serves="SEC-05", layers=ENGINEERING, unit="MB", ge=1, le=100)
    photo_max_side_px: int = key(8000, serves="SEC-05", layers=ENGINEERING, unit="px", ge=512, le=20000)
    photo_downscale_side_px: int = key(2048, serves="SEC-05", layers=ENGINEERING, unit="px", ge=256, le=8000)
    photo_jpeg_quality: int = key(85, serves="SEC-05", layers=ENGINEERING, ge=50, le=95)

    # --- durability (DAT-13; proposals) ---
    disk_low_free_mb: float = key(500.0, serves="DAT-13", layers=ENGINEERING, unit="MB", ge=50, le=100_000)
    integrity_full_check_days: float = key(7.0, serves="DAT-13", layers=ENGINEERING, unit="days", ge=1, le=90)

    # --- profiles (API-04, SEC-19; proposals) ---
    # The inactivity close: SEC-19 owns the value, and API-04 ends the profile token with it (D86).
    profile_auto_lock_s: float = key(600.0, serves="SEC-19, API-04", layers=ENGINEERING, unit="s", ge=60, le=86400)
    # Argon2id cost of a passphrase-sealed profile archive or support bundle (TABLE DAT-10a).
    passphrase_argon2_memory_kib: int = key(19456, serves="DAT-10", layers=ENGINEERING, unit="KiB", ge=8192,
                                            le=1048576)  # fmt: skip
    passphrase_argon2_iterations: int = key(2, serves="DAT-10", layers=ENGINEERING, unit="passes", ge=1, le=10)
    # --- licensing and first run (PLT-17 to PLT-20, SEC-28, UXS-10, D84, D86; proposals). Prices are never
    # configuration of the app: they are TABLE BIZ-18a's (D121), held by the checkout and the cloud gateway. ---
    # How far one app start may move the trusted time forward from the wall clock (PLT-20: a clock change never expires
    # a licence early; a machine left off for months catches up over a few starts).
    licence_clock_step_days: int = key(45, serves="PLT-20", layers=ENGINEERING, unit="days", ge=1, le=366)
    # A wall clock this far behind the trusted time is reported as set back (SEC-28); it never shortens or extends a
    # licence, which follows the trusted time.
    licence_clock_behind_hours: int = key(48, serves="SEC-28", layers=ENGINEERING, unit="h", ge=1, le=720)
    # The longest scripted first run (the one step, excluding copying model files) on the low hardware class (UXS-10).
    first_run_budget_s: float = key(120.0, serves="UXS-10", layers=ENGINEERING, unit="s", ge=10, le=600)
    # The DPIA reference every consent records (PRV-05, PRV-08); while unset, no consent can be granted (D13).
    consent_dpia_ref: str = key("", serves="PRV-05", layers=ENGINEERING)
    # Public X25519 keys (base64) of the support and study teams; exports to an unset key are refused (DAT-10, SEC-18).
    export_support_public_key: str = key("", serves="DAT-10", layers=ENGINEERING)
    export_research_public_key: str = key("", serves="DAT-10", layers=ENGINEERING)
    # Engineering: what a profile archive brought in may unpack to, in total and in entries; a larger one is refused
    # before anything is unpacked (DAT-10, SEC-18).
    archive_import_max_mb: float = key(2048.0, serves="DAT-10", layers=ENGINEERING, unit="MB", ge=1, le=65536)
    archive_import_max_entries: int = key(200_000, serves="DAT-10", layers=ENGINEERING, unit="entries", ge=10)

    # --- stores, event log, projections and traces (DAT-01, DAT-05, DAT-16, OBS-08) ---
    crash_record_retention_days: int = key(30, serves="DAT-01", layers=ENGINEERING, unit="days", ge=1, le=365)
    crash_minidump_retention_days: int = key(7, serves="DAT-01", layers=ENGINEERING, unit="days", ge=1, le=90)
    crash_reports_queued_max: int = key(20, serves="DAT-01", layers=ENGINEERING, unit="reports", ge=1, le=1000)
    sync_clock_skew_max_ms: int = key(86_400_000, serves="DAT-16", layers=ENGINEERING, unit="ms", ge=1000,
                                      le=7 * 86_400_000)  # fmt: skip
    projection_rebuild_budget_events: int = key(100_000, serves="DAT-05", layers=ENGINEERING, unit="events", ge=1)
    projection_rebuild_budget_s: float = key(10.0, serves="DAT-05", layers=ENGINEERING, unit="s", ge=0.1, le=600)
    trace_retention_days: int = key(14, serves="OBS-08", layers=ENGINEERING, unit="days", ge=1, le=365)
    trace_store_max_bytes: int = key(200 * 1024 * 1024, serves="OBS-08", layers=ENGINEERING, unit="bytes",
                                     ge=1024 * 1024)  # fmt: skip
    # --- minimal mastery projection (LRN-06, LRN-07, LRN-09, LRN-10, ASM-14); together these are the mastery
    # parameter file whose hash each projection records (LRN-02) ---
    obs_bottom_out_w: float = key(1.0, serves="LRN-06", layers=ENGINEERING, unit="weight", ge=0, le=1)
    obs_terminal_incorrect_w: float = key(0.6, serves="LRN-06", layers=ENGINEERING, unit="weight", ge=0, le=1)
    obs_partial_success_w: float = key(0.5, serves="LRN-06", layers=ENGINEERING, unit="weight", ge=0, le=1)
    obs_recovered_success_w: tuple[float, ...] = key(
        (0.5, 0.4, 0.3, 0.2, 0.1), serves="LRN-06", layers=ENGINEERING, unit="weight by highest rung 0-4"
    )
    obs_hinted_first_correct_w: tuple[float, ...] = key(
        (0.4, 0.3, 0.2, 0.1), serves="LRN-06", layers=ENGINEERING, unit="weight by highest rung 1-4"
    )
    obs_mode_check_working: float = key(0.8, serves="LRN-06", layers=ENGINEERING, unit="multiplier", ge=0, le=1)
    obs_mode_learn: float = key(0.5, serves="LRN-06", layers=ENGINEERING, unit="multiplier", ge=0, le=1)
    mastery_half_life_days: float = key(90.0, serves="LRN-07", layers=ENGINEERING, unit="days", ge=0.01, le=3650)
    mastery_k_a: float = key(0.4, serves="LRN-07", layers=ENGINEERING, unit="logit", ge=0, le=5)
    mastery_k_c: float = key(0.05, serves="LRN-07", layers=ENGINEERING, unit="per observation", ge=0, le=5)
    mastery_beta_default: float = key(0.0, serves="LRN-07", layers=ENGINEERING, unit="logit", ge=-10, le=10)
    mastery_gamma_default: float = key(0.35, serves="LRN-07", layers=ENGINEERING, unit="logit", ge=-10, le=10)
    mastery_rho_default: float = key(-0.15, serves="LRN-07", layers=ENGINEERING, unit="logit", ge=-10, le=10)
    mastery_ready_p: float = key(0.85, serves="LRN-09", layers=ENGINEERING, unit="probability", ge=0, le=1)
    mastery_ready_n_eff: float = key(3.0, serves="LRN-09", layers=ENGINEERING, unit="observations", ge=0)
    mastery_review_retrievability: float = key(0.70, serves="LRN-09", layers=ENGINEERING, unit="probability",
                                               ge=0, le=1)  # fmt: skip
    mastery_implied_w: float = key(0.3, serves="LRN-10", layers=ENGINEERING, unit="weight", ge=0, le=1)
    mastery_implied_depth: int = key(2, serves="LRN-10", layers=ENGINEERING, unit="levels", ge=0, le=5)
    mastery_check_retry_days: float = key(2.0, serves="ASM-14", layers=ENGINEERING, unit="days", ge=0, le=60)
    # --- the full learner model (M15.a-d): the rest of TABLE LRN-06a/b, FSRS (LRN-13, LRN-14), the adaptation
    # policies (LRN-16 to LRN-27), the planner (LRN-28 to LRN-30), streaks (LRN-33) and notifications (LRN-35). Each
    # is a key of mastery-params.example.yaml; "engineering" marks a value the specification leaves open. ---
    obs_first_correct_w: float = key(1.0, serves="LRN-06", layers=ENGINEERING, unit="weight", ge=0, le=1)
    obs_recovered_failure_w: float = key(0.6, serves="LRN-06", layers=ENGINEERING, unit="weight", ge=0, le=1)
    obs_abandoned_w: float = key(0.5, serves="LRN-06", layers=ENGINEERING, unit="weight", ge=0, le=1)
    obs_fast_seconds_min: float = key(3.0, serves="LRN-06", layers=ENGINEERING, unit="s", ge=0, le=60)
    obs_fast_median_share: float = key(0.2, serves="LRN-06", layers=ENGINEERING, unit="share of median", ge=0, le=1)
    obs_fast_wrong: float = key(0.3, serves="LRN-06", layers=ENGINEERING, unit="multiplier", ge=0, le=1)
    obs_fast_correct_mcq: float = key(0.5, serves="LRN-06", layers=ENGINEERING, unit="multiplier", ge=0, le=1)
    obs_slow_median_multiple: float = key(3.0, serves="LRN-06", layers=ENGINEERING, unit="multiple of median",
                                          ge=1, le=20)  # fmt: skip
    obs_slow_correct: float = key(0.8, serves="LRN-06", layers=ENGINEERING, unit="multiplier", ge=0, le=1)
    obs_diagnoser_clip: tuple[float, float] = key((0.5, 1.0), serves="LRN-06", layers=ENGINEERING,
                                                  unit="confidence (lowest, highest)")  # fmt: skip
    # The number an ad-hoc attempt's `kc_confidence` carries for the Diagnoser's stated confidence (LRN-06, AGT-01).
    diagnoser_confidence_levels: tuple[float, float, float] = key(
        (0.5, 0.75, 0.9), serves="LRN-06", layers=ENGINEERING, unit="confidence (low, medium, high)"
    )
    # LRN-11, D25: the model a subject runs (a pack's mastery-params file chooses it per subject), the four BKT
    # parameters read only by `bkt`, and the AUC margin under which pfa_elo wins the bake-off.
    mastery_model: Literal["pfa_elo", "bkt"] = key("pfa_elo", serves="LRN-11", layers=ENGINEERING)
    bkt_p_init: float = key(0.3, serves="LRN-11", layers=ENGINEERING, unit="probability", ge=0, le=1)
    bkt_p_transit: float = key(0.12, serves="LRN-11", layers=ENGINEERING, unit="probability", ge=0, le=1)
    bkt_p_slip: float = key(0.10, serves="LRN-11", layers=ENGINEERING, unit="probability", ge=0, le=0.5)
    bkt_p_guess: float = key(0.20, serves="LRN-11", layers=ENGINEERING, unit="probability", ge=0, le=0.5)
    bakeoff_auc_margin: float = key(0.01, serves="LRN-11", layers=ENGINEERING, unit="AUC", ge=0, le=0.5)
    # LRN-41: a candidate parameter file may be worse than the current one by at most this much (none by default).
    replay_gate_tolerance: float = key(0.0, serves="LRN-41", layers=ENGINEERING, unit="log loss / ECE", ge=0, le=0.1)
    calibration_bins: int = key(10, serves="LRN-11", layers=ENGINEERING, unit="bins", ge=2, le=100)  # engineering
    # LRN-13, LRN-14: one FSRS card per KC; the default FSRS-6 weights until personal calibration (LRN-12, S3).
    fsrs_desired_retention: float = key(0.9, serves="LRN-13", layers=ENGINEERING, unit="probability", ge=0.7,
                                        le=0.99)  # fmt: skip
    fsrs_weights: tuple[float, ...] = key(
        (0.212, 1.2931, 2.3065, 8.2956, 6.4133, 0.8334, 3.0194, 0.001, 1.8722, 0.1666, 0.796, 1.4835, 0.0614,
         0.2629, 1.6483, 0.6014, 1.8729, 0.5425, 0.0912, 0.0658, 0.1542),
        serves="LRN-13", layers=ENGINEERING, unit="FSRS-6 weights w0-w20",
    )  # fmt: skip
    fsrs_maximum_interval_days: int = key(36_500, serves="LRN-13", layers=ENGINEERING, unit="days", ge=1)
    fsrs_exam_window_days: int = key(14, serves="LRN-13", layers=ENGINEERING, unit="days", ge=1, le=60)
    rating_easy_time_share: float = key(0.5, serves="LRN-14", layers=ENGINEERING, unit="share of median", ge=0,
                                        le=1)  # fmt: skip
    rating_easy_min_mastery: float = key(0.9, serves="LRN-14", layers=ENGINEERING, unit="probability", ge=0, le=1)
    rating_hard_time_multiple: float = key(3.0, serves="LRN-14", layers=ENGINEERING, unit="multiple of median",
                                           ge=1, le=20)  # fmt: skip
    # Engineering: the student's local day (one review per KC per day, streak days, plan dates) is the UTC day
    # shifted by this offset; the profile carries no time zone yet.
    learner_day_offset_minutes: int = key(0, serves="LRN-13", layers=ENGINEERING, unit="min", ge=-720, le=840)
    # LRN-16: difficulty targeting, its easier target, and the weekly offline target tuning.
    adapt_target: float = key(0.825, serves="LRN-16", layers=ENGINEERING, unit="probability", ge=0.5, le=0.99)
    adapt_band: tuple[float, float] = key((0.70, 0.92), serves="LRN-16", layers=ENGINEERING,
                                          unit="probability (low, high)")  # fmt: skip
    adapt_target_supported: float = key(0.85, serves="LRN-16", layers=ENGINEERING, unit="probability", ge=0.5,
                                        le=0.99)  # fmt: skip
    adapt_tuning_tolerance: float = key(0.07, serves="LRN-16", layers=ENGINEERING, unit="probability", ge=0, le=0.3)
    adapt_tuning_step: float = key(0.02, serves="LRN-16", layers=ENGINEERING, unit="probability", ge=0, le=0.05)
    adapt_tuning_max_offset: float = key(0.05, serves="LRN-16", layers=ENGINEERING, unit="probability", ge=0,
                                         le=0.05)  # fmt: skip
    adapt_tuning_weeks: int = key(2, serves="LRN-16", layers=ENGINEERING, unit="weeks", ge=1, le=8)
    frustration_half_life_events: float = key(10.0, serves="LRN-16", layers=ENGINEERING, unit="events", ge=1,
                                              le=200)  # fmt: skip
    frustration_high: float = key(0.6, serves="LRN-16", layers=ENGINEERING, unit="score", ge=0, le=1)
    confidence_bias_window: int = key(20, serves="LRN-16", layers=ENGINEERING, unit="rated attempts", ge=1, le=200)
    # LRN-17: worked-example fading.
    example_full_below: float = key(0.4, serves="LRN-17", layers=ENGINEERING, unit="probability", ge=0, le=1)
    example_problems_from: float = key(0.7, serves="LRN-17", layers=ENGINEERING, unit="probability", ge=0, le=1)
    fading_max_stage: int = key(10, serves="LRN-17", layers=ENGINEERING, unit="blank steps", ge=1, le=10)
    # LRN-18: the hint start rung.
    hint_concept_below: float = key(0.4, serves="LRN-18", layers=ENGINEERING, unit="probability", ge=0, le=1)
    hint_dependence_window: int = key(6, serves="LRN-18", layers=ENGINEERING, unit="opportunities", ge=1, le=50)
    hint_dependence_share: float = key(0.5, serves="LRN-18", layers=ENGINEERING, unit="share", ge=0, le=1)
    hint_dependence_rung: int = key(3, serves="LRN-18", layers=ENGINEERING, unit="rung", ge=1, le=5)
    # LRN-19: gaming detection.
    gaming_hint_gap_s: float = key(10.0, serves="LRN-19", layers=ENGINEERING, unit="s", ge=0, le=120)
    gaming_fast_wrong_run: int = key(3, serves="LRN-19", layers=ENGINEERING, unit="answers", ge=2, le=10)
    gaming_break_flags: int = key(3, serves="LRN-19", layers=ENGINEERING, unit="flags per session", ge=1, le=20)
    # LRN-20: unprompted help.
    offer_idle_s: float = key(90.0, serves="LRN-20", layers=ENGINEERING, unit="s", ge=10, le=900, client=True)
    offer_after_wrong: int = key(2, serves="LRN-20", layers=ENGINEERING, unit="wrong attempts", ge=1, le=10)
    # LRN-21: self-explanation cadence.
    self_explanation_every_steps: int = key(4, serves="LRN-21", layers=ENGINEERING, unit="steps", ge=1, le=50)
    self_explanation_band: tuple[float, float] = key((0.4, 0.85), serves="LRN-21", layers=ENGINEERING,
                                                     unit="probability (low, high)")  # fmt: skip
    # LRN-22: representation switching.
    representation_trigger: int = key(2, serves="LRN-22", layers=ENGINEERING, unit="signals", ge=1, le=10)
    representation_outcome_attempts: int = key(2, serves="LRN-22", layers=ENGINEERING, unit="attempts", ge=1,
                                               le=10)  # fmt: skip
    # LRN-23: prerequisite detours.
    detour_failures: int = key(2, serves="LRN-23", layers=ENGINEERING, unit="failures", ge=1, le=10)
    detour_prereq_below: float = key(0.5, serves="LRN-23", layers=ENGINEERING, unit="probability", ge=0, le=1)
    detour_minutes: int = key(5, serves="LRN-23", layers=ENGINEERING, unit="min", ge=1, le=30)
    detour_items: int = key(2, serves="LRN-23", layers=ENGINEERING, unit="items", ge=1, le=10)
    detour_max_depth: int = key(2, serves="LRN-23", layers=ENGINEERING, unit="detours", ge=1, le=2)
    # LRN-24, LRN-25: block, then interleave; the exam-proximity shift.
    block_until: float = key(0.6, serves="LRN-24", layers=ENGINEERING, unit="probability", ge=0, le=1)
    interleave_max_run: int = key(2, serves="LRN-24", layers=ENGINEERING, unit="items", ge=1, le=10)
    countdown_interleave_share: float = key(0.5, serves="LRN-25", layers=ENGINEERING, unit="share", ge=0, le=1)
    final_recent_error_days: int = key(60, serves="LRN-25", layers=ENGINEERING, unit="days", ge=1, le=365)
    # LRN-26: the stuck cap's session count and revisit (the opportunity cap and bar are stuck_cap_* above).
    stuck_cap_sessions: int = key(3, serves="LRN-26", layers=ENGINEERING, unit="sessions", ge=1, le=20)
    stuck_revisit_days: float = key(3.0, serves="LRN-26", layers=ENGINEERING, unit="days", ge=0.5, le=60)
    # LRN-27: support for weaker students.
    support_diagnostic_below: float = key(0.35, serves="LRN-27", layers=ENGINEERING, unit="probability", ge=0,
                                          le=1)  # fmt: skip
    support_min_days_per_week: int = key(3, serves="LRN-27", layers=ENGINEERING, unit="days", ge=1, le=7)
    support_weeks: int = key(2, serves="LRN-27", layers=ENGINEERING, unit="weeks", ge=1, le=8)
    support_example_below: float = key(0.5, serves="LRN-27", layers=ENGINEERING, unit="probability", ge=0, le=1)
    support_activity_minutes: int = key(20, serves="LRN-27", layers=ENGINEERING, unit="min (estimate only, D54)",
                                        ge=5, le=120)  # fmt: skip
    support_weekly_freezes: int = key(2, serves="LRN-27", layers=ENGINEERING, unit="freezes", ge=0, le=2)
    # LRN-28 to LRN-30: the planner. Durations size suggestions only; nothing here is a limit (D54).
    planner_main_minutes: tuple[int, int] = key((15, 20), serves="LRN-28", layers=ENGINEERING,
                                                unit="min (practice, lesson)")  # fmt: skip
    planner_mixed_minutes: int = key(5, serves="LRN-28", layers=ENGINEERING, unit="min", ge=1, le=60)
    planner_reflect_minutes: int = key(1, serves="LRN-28", layers=ENGINEERING, unit="min", ge=1, le=10)
    # Engineering: one recall review's estimate; ten make the specification's "about 5 minutes" warm-up.
    planner_review_item_minutes: float = key(0.5, serves="LRN-28", layers=ENGINEERING, unit="min", ge=0.1, le=10)
    # Engineering: the items that complete a main or mixed block (a warm-up completes with its listed reviews).
    planner_main_items: int = key(5, serves="LRN-31", layers=ENGINEERING, unit="items", ge=1, le=50)
    planner_mixed_items: int = key(4, serves="LRN-31", layers=ENGINEERING, unit="items", ge=1, le=50)
    planner_alternatives: int = key(3, serves="LRN-31", layers=ENGINEERING, unit="alternatives", ge=1, le=3)
    planner_max_subjects: int = key(2, serves="LRN-28", layers=ENGINEERING, unit="subjects", ge=1, le=5)
    planner_prerequisite_ready: float = key(0.6, serves="LRN-28", layers=ENGINEERING, unit="probability", ge=0,
                                            le=1)  # fmt: skip
    planner_recent_failure_boost: float = key(0.5, serves="LRN-28", layers=ENGINEERING, unit="factor", ge=0, le=5)
    review_cap_items: int = key(15, serves="LRN-29", layers=ENGINEERING, unit="reviews", ge=1, le=200)
    review_cap_share: float = key(0.3, serves="LRN-29", layers=ENGINEERING, unit="share of the plan", ge=0.05,
                                  le=1)  # fmt: skip
    review_backlog_days: int = key(7, serves="LRN-29", layers=ENGINEERING, unit="days", ge=1, le=60)
    phase_countdown_days: int = key(56, serves="LRN-30", layers=ENGINEERING, unit="days", ge=2, le=365)
    phase_final_days: int = key(14, serves="LRN-30", layers=ENGINEERING, unit="days", ge=1, le=60)
    exam_day_review_minutes: int = key(15, serves="LRN-30", layers=ENGINEERING, unit="min", ge=1, le=60)
    # Engineering: the countdown phase's weekly timed mixed paper (LRN-30) is this long, and so is a final-phase mock.
    phase_mock_minutes: int = key(45, serves="LRN-30", layers=ENGINEERING, unit="min", ge=5, le=240)
    phase_mock_every_days: int = key(7, serves="LRN-30", layers=ENGINEERING, unit="days", ge=1, le=30)
    # LRN-33: streaks with freeze.
    streak_min_minutes: float = key(5.0, serves="LRN-33", layers=ENGINEERING, unit="min of checked work", ge=0.5,
                                    le=120)  # fmt: skip
    streak_weekly_freezes: int = key(1, serves="LRN-33", layers=ENGINEERING, unit="freezes", ge=0, le=2)
    streak_max_freezes: int = key(2, serves="LRN-33", layers=ENGINEERING, unit="freezes", ge=0, le=2)
    # LRN-35: notifications (a fixed default, never a setting, D53).
    notify_quiet_hours: tuple[int, int] = key((21, 7), serves="LRN-35", layers=ENGINEERING,
                                              unit="local hour (start, end)")  # fmt: skip
    notify_max_per_day: int = key(1, serves="LRN-35", layers=ENGINEERING, unit="notifications", ge=0, le=1)
    # LRN-42: the learner model's time budgets on the low hardware class (the rebuild budget is projection_rebuild_*).
    budget_update_ms: float = key(50.0, serves="LRN-42", layers=ENGINEERING, unit="ms", ge=1, le=5000)
    budget_plan_ms: float = key(500.0, serves="LRN-42", layers=ENGINEERING, unit="ms", ge=1, le=10_000)
    # Engineering: the weeks of mastery history the Progress screen charts (UXS-27).
    progress_history_weeks: int = key(8, serves="UXS-27", layers=ENGINEERING, unit="weeks", ge=1, le=52)
    # TCH-12: the weekly parent digest is due on Sunday at this local hour (proposal: 18:00; fixed, not a setting, D53);
    # a week below the minutes reads "Not enough activity this week to summarise" (JRN-13 F1; engineering); a KC
    # learning for over the days is one to watch (proposal: 2 weeks).
    parent_digest_hour: int = key(18, serves="TCH-12", layers=ENGINEERING, unit="h", ge=0, le=23)
    parent_digest_min_minutes: int = key(20, serves="TCH-12", layers=ENGINEERING, unit="min", ge=0, le=600)
    parent_watch_learning_days: int = key(14, serves="TCH-12", layers=ENGINEERING, unit="days", ge=1, le=365)
    # TCH-14, TABLE TCH-14a (proposal values): `inactive` after days without a session; `bottom_out_heavy` over this
    # share of a week's steps ended at the bottom-out hint, once the week has the fewest steps (engineering); a trigger
    # older than the look-back is not raised when the alerts are first read (engineering).
    alert_inactive_days: int = key(7, serves="TCH-14", layers=ENGINEERING, unit="days", ge=1, le=90)
    alert_bottom_out_share: float = key(0.4, serves="TCH-14", layers=ENGINEERING, unit="share", ge=0, le=1)
    alert_bottom_out_min_steps: int = key(5, serves="TCH-14", layers=ENGINEERING, unit="steps", ge=1, le=1000)
    alert_lookback_days: int = key(14, serves="TCH-14", layers=ENGINEERING, unit="days", ge=1, le=365)
    # M16.b-d policy defaults (ASM-10 to ASM-24, LOC-24).
    diagnostic_max_items: int = key(15, serves="ASM-10", layers=ENGINEERING, ge=1)
    diagnostic_max_minutes: int = key(12, serves="ASM-10", layers=ENGINEERING, ge=1)
    diagnostic_not_learned_weight: float = key(0.5, serves="ASM-10", layers=ENGINEERING, ge=0, le=1)
    diagnostic_min_items: int = key(10, serves="ASM-11", layers=ENGINEERING, ge=1)
    diagnostic_resolved_distance: float = key(0.3, serves="ASM-11", layers=ENGINEERING, ge=0, le=0.5)
    probe_options: int = key(4, serves="ASM-12", layers=ENGINEERING, ge=2, le=8)
    probe_analysis_min_responses: int = key(200, serves="ASM-12", layers=ENGINEERING, ge=1)
    probe_distractor_min_share: float = key(0.02, serves="ASM-12", layers=ENGINEERING, ge=0, le=1)
    probe_unmatched_errors: int = key(2, serves="ASM-13", layers=ENGINEERING, ge=1)
    mastery_check_items: int = key(3, serves="ASM-14", layers=ENGINEERING, ge=3, le=3)
    mastery_check_min_spacing_hours: float = key(24, serves="ASM-14", layers=ENGINEERING, ge=0)
    practice_exposure_days: int = key(14, serves="ASM-22", layers=ENGINEERING, ge=1)
    # ASM-16, UXS-25: the pause budget; the sitting screen says how many pauses and how much pause time are left.
    mock_pause_count: int = key(2, serves="ASM-16", layers=ENGINEERING, unit="pauses", ge=0, le=2, client=True)
    mock_pause_minutes: int = key(10, serves="ASM-16", layers=ENGINEERING, unit="minutes", ge=0, client=True)
    mock_abandon_days: int = key(7, serves="ASM-16", layers=ENGINEERING, unit="days", ge=1, client=True)
    mock_checkpoint_ms: int = key(250, serves="ASM-16", layers=ENGINEERING, ge=1, le=1000)
    mock_method_reference_cases: int = key(20, serves="ASM-17", layers=ENGINEERING, ge=1)
    mock_method_agreement_min: float = key(0.95, serves="ASM-17", layers=ENGINEERING, ge=0, le=1)
    mock_focus_loss_ms: int = key(30_000, serves="ASM-18", layers=ENGINEERING, ge=0)
    mock_large_paste_chars: int = key(200, serves="ASM-18", layers=ENGINEERING, ge=0)
    # UXS-25: how many of a profile's sittings `GET /v1/mocks` lists, newest first.
    mock_list_sittings_max: int = key(20, serves="UXS-25", layers=ENGINEERING, unit="sittings", ge=1, le=100)
    # UXS-25, ASM-16: the sitting screen saves an answer this long after the last change (autosave within 1 s) and reads
    # the sitting again this often while it runs, so the clock it shows stays the core's.
    ui_mock_save_ms: int = key(400, serves="ASM-16", layers=ENGINEERING, unit="ms", ge=50, le=1000, client=True)
    ui_mock_refresh_ms: int = key(15_000, serves="UXS-25", layers=ENGINEERING, unit="ms", ge=1000, le=60_000,
                                  client=True)  # fmt: skip
    # UXS-51, ASM-22: the most matches one worked-solution search returns (each is recorded as seen); a typed query is
    # answered within `search_offline_ms` on the low class with no model (the acceptance budget).
    search_results_max: int = key(20, serves="UXS-51", layers=ENGINEERING, unit="matches", ge=1, le=100)
    search_offline_ms: int = key(1000, serves="UXS-51", layers=ENGINEERING, unit="ms", ge=50, le=10_000)
    grade_min_checked: int = key(30, serves="ASM-19", layers=ENGINEERING, ge=1)
    grade_min_coverage: float = key(0.6, serves="ASM-19", layers=ENGINEERING, ge=0, le=1)
    grade_draws: int = key(1000, serves="ASM-19", layers=ENGINEERING, ge=1)
    grade_recent_days: int = key(30, serves="ASM-19", layers=ENGINEERING, ge=1)
    grade_mock_blend: float = key(0.5, serves="ASM-19", layers=ENGINEERING, ge=0, le=1)
    grade_interval_share: float = key(0.8, serves="ASM-19", layers=ENGINEERING, ge=0, le=1)
    grade_max_band_span: int = key(3, serves="ASM-19", layers=ENGINEERING, ge=1)
    grade_boundaries: tuple[float, ...] = key((0.75, 0.65, 0.5, 0.4), serves="ASM-19", layers=ENGINEERING)
    mock_review_items_per_kc: int = key(1, serves="ASM-20", layers=ENGINEERING, ge=1, le=40)
    mock_weakest_kcs: int = key(3, serves="ASM-20", layers=ENGINEERING, ge=1)
    assessment_working_max_lines: int = key(100, serves="ASM-17", layers=ENGINEERING, ge=1, le=100)
    assessment_working_max_chars: int = key(4000, serves="ASM-17", layers=ENGINEERING, ge=1, le=4000)
    short_answer_max_chars: int = key(4000, serves="API-27", layers=ENGINEERING, ge=1, le=4000)
    # M45.c (ASM-27, LRN-44; D119). Engineering: the bounds of one imported past paper, the KCs one question links to,
    # and the papers a profile keeps.
    past_paper_questions_max: int = key(200, serves="ASM-27", layers=ENGINEERING, unit="question parts", ge=1, le=200)
    past_paper_question_kcs_max: int = key(5, serves="ASM-27", layers=ENGINEERING, unit="KCs", ge=1, le=20)
    past_papers_max: int = key(200, serves="ASM-27", layers=ENGINEERING, unit="papers", ge=1, le=200)
    # LRN-44: the plan front-loads the nearest exam's subject this many days before it; reminders `proposal:` 14, 7, 3
    # and 1 days before and the morning of (from this local hour).
    exam_frontload_days: int = key(21, serves="LRN-44", layers=ENGINEERING, unit="days", ge=1, le=120)
    exam_reminder_days: tuple[int, ...] = key((14, 7, 3, 1), serves="LRN-44", layers=ENGINEERING, unit="days before")
    exam_reminder_morning_hour: int = key(6, serves="LRN-44", layers=ENGINEERING, unit="local hour", ge=0, le=12)
    exam_dates_max: int = key(40, serves="LRN-44", layers=ENGINEERING, unit="exam dates", ge=1, le=40)
    # --- learning sessions (LRN-01) ---
    session_idle_timeout_min: float = key(30.0, serves="LRN-01", layers=ENGINEERING, unit="min", ge=1, le=600)
    # --- memory: notes, the learner block, recall and the Reflector (M15 design §5). Also used: retention_note_days
    # and retention_transcript_days. Defaults marked "engineering" have no specification proposal: chosen here. ---
    notes_max_chars: int = key(140, serves="LRN-38", layers=ENGINEERING, unit="characters", ge=20, le=140)
    notes_max_active: int = key(50, serves="LRN-38", layers=ENGINEERING, unit="notes", ge=1, le=50)
    notes_max_per_subject: int = key(40, serves="MEM-12", layers=ENGINEERING, unit="notes", ge=1, le=50)
    notes_max_chars_per_subject: int = key(6000, serves="MEM-12", layers=ENGINEERING, unit="characters", ge=140,
                                           le=7000)  # fmt: skip
    # Engineering: open review items (contradictions and proposed changes to the student's own notes) per subject; a
    # further one is dropped and counted, never stored, until the student answers one.
    notes_max_reviews_per_subject: int = key(3, serves="MEM-09", layers=ENGINEERING, unit="review items", ge=1,
                                             le=10)  # fmt: skip
    memory_idle_write_min: float = key(15.0, serves="MEM-06", layers=ENGINEERING, unit="min", ge=1, le=600)
    # Engineering: long enough to read the saved line and reach Undo, short enough that it reads as part of the turn.
    memory_undo_window_s: float = key(30.0, serves="MEM-11", layers=ENGINEERING, unit="s", ge=5, le=600)
    snapshot_recent_notes: int = key(2, serves="MEM-04", layers=ENGINEERING, unit="notes", ge=0, le=10)
    snapshot_top_kcs: int = key(3, serves="LRN-05", layers=ENGINEERING, unit="KCs", ge=0, le=10)
    snapshot_misconceptions_max: int = key(3, serves="LRN-05", layers=ENGINEERING, unit="misconceptions", ge=0,
                                           le=10)  # fmt: skip
    # Engineering: the hint-use band of a KC from the share of its attempts that used a hint (high at or above the
    # first, medium at or above the second, else low).
    snapshot_hint_use_high: float = key(0.5, serves="LRN-05", layers=ENGINEERING, unit="share of attempts", ge=0,
                                        le=1)  # fmt: skip
    snapshot_hint_use_medium: float = key(0.2, serves="LRN-05", layers=ENGINEERING, unit="share of attempts", ge=0,
                                          le=1)  # fmt: skip
    memory_index_line_chars: int = key(60, serves="MEM-13", layers=ENGINEERING, unit="characters", ge=20, le=140)
    session_recall_max_matches: int = key(5, serves="MEM-18", layers=ENGINEERING, unit="matches", ge=1, le=20)
    session_recall_match_chars: int = key(300, serves="MEM-18", layers=ENGINEERING, unit="characters", ge=40,
                                          le=2000)  # fmt: skip
    # Engineering: a few words of a value or phrase; the tool schema's 200 characters is the outer bound.
    session_recall_pattern_max_chars: int = key(100, serves="MEM-18", layers=ENGINEERING, unit="characters", ge=1,
                                                le=200)  # fmt: skip
    reflector_yield_tolerance: float = key(0.10, serves="AGT-26", layers=ENGINEERING, unit="share", ge=0, le=1)
    # Engineering: wait this long after the last turn before a background Reflector job may start.
    reflector_idle_delay_s: float = key(20.0, serves="AGT-26", layers=ENGINEERING, unit="s", ge=0, le=3600)
    # Engineering: numbered evidence lines in one Reflector task, so the task fits the low class's context.
    reflector_max_evidence_lines: int = key(40, serves="AGT-26", layers=ENGINEERING, unit="lines", ge=1, le=200)
    # Engineering: ended sessions without a summary picked up at startup.
    reflector_backlog_sessions: int = key(3, serves="AGT-26", layers=ENGINEERING, unit="sessions", ge=0, le=50)
    # Engineering: how often a job paused by the student's turns (or a forget) is started again before it is left
    # for the next write point, so a busy session cannot keep one job cycling.
    reflector_max_starts: int = key(4, serves="AGT-26", layers=ENGINEERING, unit="starts", ge=1, le=20)
    # Engineering: the classifier's probability at which a candidate note is blocked.
    capture_classifier_threshold: float = key(0.5, serves="MEM-10", layers=ENGINEERING, unit="probability", ge=0,
                                              le=1)  # fmt: skip
    # Engineering: a run of this many of the student's own words in a note counts as a quote.
    capture_quote_max_words: int = key(6, serves="MEM-10", layers=ENGINEERING, unit="words", ge=2, le=40)
    # Engineering: a word this long or longer one edit away from a listed word (and not a known near miss) matches
    # it, so a misspelt sensitive word is still caught.
    capture_fuzzy_min_letters: int = key(6, serves="MEM-10", layers=ENGINEERING, unit="letters", ge=4, le=20)
    # Engineering: the share of a note's words (three letters or more) that must read as English. The lexicon is
    # English only, so a note mostly in another language is blocked until that language has its own (MEM-25).
    capture_english_min_share: float = key(0.6, serves="MEM-10", layers=ENGINEERING, unit="share of words", ge=0,
                                           le=1)  # fmt: skip
    misconception_active_evidence: int = key(2, serves="PED-22", layers=ENGINEERING, unit="matching events", ge=1,
                                             le=10)  # fmt: skip
    misconception_dispute_reactivate: int = key(2, serves="LRN-37", layers=ENGINEERING, unit="matching events",
                                                ge=1, le=10)  # fmt: skip
    # --- tutor flows, the hint ladder and solution policy (M14.d; proposals) ---
    hint_min_gap_s: float = key(10.0, serves="PED-06", layers=ENGINEERING, unit="s", ge=0, le=120)
    bottom_out_review_days: tuple[int, int] = key((1, 3), serves="PED-07", layers=ENGINEERING,
                                                  unit="days (earliest, latest) for the isomorphic item")  # fmt: skip
    misconception_fixes_before_save: int = key(3, serves="PED-24", layers=ENGINEERING, unit="fixes", ge=1, le=10)
    # The release stage whose engine flows the build runs (AGT-15, AGT-19): from S2 the Diagnoser runs before the
    # Solver, the Checker and the Diagnoser read an answer the code could not confirm, the guided and misconception
    # flows drive the turn and the input-guard and rule-matcher roles run. Engineering sets it for the build; a
    # developer override (`JANA_BUILD_STAGE`, development builds only) keeps the S1 path testable.
    build_stage: Literal["S1", "S2", "S3", "S4", "S5"] = key("S2", serves="AGT-15, AGT-19",
                                                             layers=(Layer.DEFAULTS, Layer.DEVELOPER))  # fmt: skip
    # LRN-26: this many opportunities on one KC without its mastery reaching the bar is the stuck cap.
    stuck_cap_opportunities: int = key(8, serves="LRN-26", layers=ENGINEERING, unit="opportunities", ge=1, le=50)
    stuck_cap_mastery: float = key(0.6, serves="LRN-26", layers=ENGINEERING, unit="probability", ge=0, le=1)
    # A wrong answer raises the rung by one up to this rung; rung 5, the bottom-out, is only ever asked for (AGT-19,
    # PED-07, UXS-17).
    wrong_answer_max_rung: int = key(4, serves="AGT-19", layers=ENGINEERING, unit="rung", ge=1, le=4)
    revision_max_rung: int = key(2, serves="PED-25", layers=ENGINEERING, unit="rung", ge=1, le=5)
    gate_check_working_first_error: float = key(0.85, serves="AGT-21", layers=ENGINEERING, unit="share", ge=0.5,
                                                le=1)  # fmt: skip
    # --- the soft rule engine (M14.e, PED-15; proposals) ---
    rules_judged_max: int = key(12, serves="PED-15", layers=ENGINEERING, unit="rules", ge=0, le=50)
    rules_injected_max: int = key(6, serves="PED-15", layers=ENGINEERING, unit="rules", ge=0, le=20)
    rules_injected_tokens: int = key(300, serves="PED-15", layers=ENGINEERING, unit="tokens", ge=0, le=2000)
    rules_matcher_output_tokens: int = key(400, serves="PED-15", layers=ENGINEERING, unit="tokens", ge=16, le=4000)
    rules_matcher_timeout_s: float = key(3.0, serves="PED-15", layers=ENGINEERING, unit="s", ge=0.1, le=30)
    # --- safety policy in code (M14.f; proposals) ---
    bulk_paste_min_questions: int = key(3, serves="PED-19", layers=ENGINEERING, unit="questions", ge=2, le=20)
    guard_classifier_threshold: float = key(0.5, serves="PRV-18", layers=ENGINEERING, unit="score", ge=0, le=1)
    # A message the guard cannot read (PRV-14, JRN-10 F2): at least this many words, under this share of them common
    # English words and most ending in a vowel, fails closed to the distress template unless the input-guard role
    # classified it with confidence.
    guard_language_min_words: int = key(4, serves="PRV-14", layers=ENGINEERING, unit="words", ge=2, le=50)
    guard_language_min_share: float = key(0.15, serves="PRV-14", layers=ENGINEERING, unit="share", ge=0, le=1)
    guard_language_vowel_share: float = key(0.7, serves="PRV-14", layers=ENGINEERING, unit="share", ge=0, le=1)
    # --- deterministic marking of numeric answers (ASM-04) ---
    numeric_default_relative_tolerance: float = key(0.005, serves="ASM-04", layers=ENGINEERING, unit="share",
                                                    ge=0, le=0.5)  # fmt: skip
    numeric_sig_figs_slack: int = key(1, serves="ASM-04", layers=ENGINEERING, unit="significant figures", ge=0,
                                      le=3)  # fmt: skip
    # --- deterministic marking of chemical equations (CUR-15, ASM-03): marks a part loses for a balanced equation
    # not in lowest terms when the item allows partial credit, and for missing or wrong state symbols the item
    # requires (engineering) ---
    marking_chem_not_lowest_marks_lost: int = key(1, serves="CUR-15", layers=ENGINEERING, unit="marks", ge=0, le=20)
    marking_chem_state_symbols_marks_lost: int = key(1, serves="CUR-15", layers=ENGINEERING, unit="marks", ge=0,
                                                     le=20)  # fmt: skip
    # --- deterministic marking of free-response maths (ASM-05, ASM-03): marks an algebraic answer of the right value
    # loses when it is not in the form the item asks for (unless the item makes that wrong), and the most values one
    # equation-solutions answer may list before it is unreadable (each is checked against every key value) ---
    marking_algebraic_form_marks_lost: int = key(1, serves="ASM-05", layers=ENGINEERING, unit="marks", ge=0, le=20)
    marking_solutions_max_values: int = key(10, serves="ASM-03", layers=ENGINEERING, unit="values", ge=1, le=50)
    # --- diagram labels (VSL-28, ASM-08): the edits a typed label may differ from a part's name or alias by, and the
    # letters a name needs before any edit is tolerated; the share of the seeded misconception set whose signatures
    # must name the intended misconception (CUR-18, TABLE EVL-17a) ---
    marking_label_edit_tolerance: int = key(1, serves="VSL-28", layers=ENGINEERING, unit="edits", ge=0, le=3)
    marking_label_min_letters: int = key(5, serves="VSL-28", layers=ENGINEERING, unit="letters", ge=1, le=20)
    marking_signature_match_share: float = key(0.95, serves="CUR-18", layers=ENGINEERING, unit="share", ge=0.5,
                                               le=1)  # fmt: skip
    # --- the model-assisted quantities pass of the authoring tool (CUR-18, D90; the owner's decision of 2026-10-16):
    # the spend it may never pass over all its runs, and the reply size and wait of one proposal request ---
    quantity_proposal_budget_usd: float = key(2.0, serves="CUR-18", layers=ENGINEERING, unit="USD", ge=0, le=20)
    quantity_proposal_max_output_tokens: int = key(1024, serves="CUR-18", layers=ENGINEERING, unit="tokens", ge=64,
                                                   le=8192)  # fmt: skip
    quantity_proposal_timeout_s: float = key(60.0, serves="CUR-18", layers=ENGINEERING, unit="s", ge=5, le=600)
    quantity_proposal_workers: int = key(4, serves="CUR-18", layers=ENGINEERING, unit="requests", ge=1, le=16)
    # --- S1 coverage target a subject-level must meet before students see it (CUR-31, TABLE CUR-31a) ---
    coverage_s1_items_per_kc: int = key(15, serves="CUR-31", layers=ENGINEERING, unit="items", ge=1)
    coverage_s1_misconceptions_per_kc: int = key(2, serves="CUR-31", layers=ENGINEERING, unit="misconceptions", ge=0)
    coverage_s1_worked_examples_per_kc: int = key(2, serves="CUR-31", layers=ENGINEERING, unit="worked examples",
                                                  ge=0)  # fmt: skip
    coverage_s1_lessons: int = key(1, serves="CUR-31", layers=ENGINEERING, unit="lessons", ge=0)
    # --- S2 coverage target per subject-level (CUR-31, TABLE CUR-31a; counts of published records) ---
    coverage_s2_items_per_kc: int = key(10, serves="CUR-31", layers=ENGINEERING, unit="items", ge=1)
    coverage_s2_diagnostic_items_per_kc: int = key(3, serves="CUR-31", layers=ENGINEERING, unit="diagnostic items",
                                                   ge=0)  # fmt: skip
    coverage_s2_misconceptions_per_kc: int = key(2, serves="CUR-31", layers=ENGINEERING, unit="misconceptions", ge=0)
    # A KC needs the misconceptions above only from this exam weight (TABLE CUR-31a; as CUR-16 and CUR-18).
    coverage_s2_misconception_min_kc_weight: float = key(0.05, serves="CUR-31", layers=ENGINEERING, unit="weight",
                                                         ge=0, le=1)  # fmt: skip
    coverage_s2_worked_examples_per_kc: int = key(1, serves="CUR-31", layers=ENGINEERING, unit="worked examples",
                                                  ge=0)  # fmt: skip
    coverage_s2_lesson_topics: int = key(10, serves="CUR-31", layers=ENGINEERING, unit="highest-weight topics", ge=0)
    coverage_s2_mock_papers: int = key(3, serves="CUR-31", layers=ENGINEERING, unit="mock papers", ge=0)
    # Engineering: how far a mock paper's blueprint topic weights may sum from one, checked by the pack builder.
    mock_blueprint_weight_tolerance: float = key(0.02, serves="ASM-15", layers=ENGINEERING, unit="share", ge=0,
                                                 le=0.2)  # fmt: skip
    # --- default mock formats (ASM-15, TABLE ASM-15a), used by the mock-paper generator: O level Paper 1's
    # multiple-choice count and time, Paper 2's time, a section mock's time; and the share of a paper's time its
    # questions' estimated working time may fill, the rest left for reading and checking (engineering) ---
    mock_o_paper1_questions: int = key(40, serves="ASM-15", layers=ENGINEERING, unit="questions", ge=1, le=100)
    mock_o_paper1_minutes: int = key(60, serves="ASM-15", layers=ENGINEERING, unit="minutes", ge=10, le=240)
    mock_o_paper2_minutes: int = key(120, serves="ASM-15", layers=ENGINEERING, unit="minutes", ge=10, le=240)
    mock_section_minutes: int = key(30, serves="ASM-15", layers=ENGINEERING, unit="minutes", ge=10, le=240)
    mock_working_time_share: float = key(0.8, serves="ASM-15", layers=ENGINEERING, unit="share", ge=0.1, le=1)
    # The share of each subject's item bank reserved for mocks and never served in practice (ASM-22).
    mock_reserved_bank_share: float = key(0.2, serves="ASM-22", layers=ENGINEERING, unit="share", ge=0, le=1)
    # --- curriculum map and knowledge components, checked by the pack builder (CUR-03, CUR-04) ---
    curriculum_topic_weight_tolerance: float = key(0.02, serves="CUR-03", layers=ENGINEERING, unit="share", ge=0,
                                                   le=0.2)  # fmt: skip
    kc_statement_max_chars: int = key(140, serves="CUR-04", layers=ENGINEERING, unit="characters", ge=20, le=400)
    kc_per_subject_level_min: int = key(150, serves="CUR-04", layers=ENGINEERING, unit="KCs", ge=1)
    kc_per_subject_level_max: int = key(300, serves="CUR-04", layers=ENGINEERING, unit="KCs", ge=1)
    # Engineering: a build check whose symbolic time slice ran out (often a cold SymPy cache on a loaded machine) runs
    # this many times in all before the build reports it; a timeout is never read as a result (VER-27).
    build_check_timeout_attempts: int = key(3, serves="VER-27", layers=ENGINEERING, unit="attempts", ge=1, le=10)
    # Offline pack verification has its own bounded slice for exact symbolic calculations.
    build_check_symbolic_slice_ms: int = key(3000, serves="VER-27", layers=ENGINEERING, unit="ms", ge=100, le=5000)
    # --- lesson shape, checked by the pack builder (CUR-11, TABLE CUR-11a) ---
    lesson_idea_max_words: int = key(120, serves="CUR-11", layers=ENGINEERING, unit="words", ge=10, le=1000)
    lesson_summary_max_words: int = key(60, serves="CUR-11", layers=ENGINEERING, unit="words", ge=5, le=500)
    lesson_min_minutes: int = key(8, serves="CUR-11", layers=ENGINEERING, unit="minutes", ge=1, le=60)
    lesson_max_minutes: int = key(15, serves="CUR-11", layers=ENGINEERING, unit="minutes", ge=1, le=60)
    # --- step-shape lessons (CUR-11, TABLE CUR-11b, D110) ---
    lesson_steps_min: int = key(3, serves="CUR-11", layers=ENGINEERING, unit="steps", ge=1, le=20)
    lesson_steps_max: int = key(7, serves="CUR-11", layers=ENGINEERING, unit="steps", ge=1, le=20)
    lesson_step_text_max_sentences: int = key(3, serves="CUR-11", layers=ENGINEERING, unit="sentences", ge=1, le=10)
    lesson_step_why_max_sentences: int = key(2, serves="CUR-11", layers=ENGINEERING, unit="sentences", ge=1, le=10)
    # Misses on a step or check item before the player shows it with its working (UXS-23, MATHia's rule).
    lesson_step_misses_before_shown: int = key(2, serves="UXS-23", layers=ENGINEERING, unit="misses", ge=1, le=5)
    # --- automated content QA before publication (CUR-25, TABLE CUR-25a) ---
    # Readability grade (Flesch-Kincaid) of stems and lesson text, maths and KC glossary terms left out.
    qa_reading_grade_max_o: float = key(8.0, serves="CUR-25", layers=ENGINEERING, unit="grade", ge=1, le=20)
    qa_reading_grade_max_a: float = key(10.0, serves="CUR-25", layers=ENGINEERING, unit="grade", ge=1, le=20)
    qa_near_duplicate_similarity: float = key(0.92, serves="CUR-25", layers=ENGINEERING, unit="cosine", ge=0.5,
                                              le=1)  # fmt: skip
    # The key position's share may differ from uniform by this fraction of uniform (per subject-level).
    qa_mcq_key_balance_tolerance: float = key(0.10, serves="CUR-25", layers=ENGINEERING, unit="share of uniform",
                                              ge=0, le=1)  # fmt: skip
    # Engineering: the fewest multiple-choice keys with one option count for their spread to be judged.
    qa_mcq_balance_min_items: int = key(50, serves="CUR-25", layers=ENGINEERING, unit="items", ge=1)
    # Engineering: the shortest alt text a library diagram or preset may have.
    qa_alt_text_min_words: int = key(5, serves="CUR-25", layers=ENGINEERING, unit="words", ge=1, le=100)
    # --- content safety review before publication (CUR-27): the share of a release's new or changed records sampled
    # for the signed review; records on sensitive topics are always reviewed ---
    safety_review_sample_share: float = key(0.10, serves="CUR-27", layers=ENGINEERING, unit="share", ge=0, le=1)
    # --- pack releases: size and update budgets, measured per release (CUR-34) ---
    pack_size_budget_mb: float = key(80.0, serves="CUR-34", layers=ENGINEERING, unit="MB", ge=1, le=4096)
    # Engineering: the largest pack file the app opens at all; an untrusted file past it is refused before it is read
    # or its signature checked (SEC-13).
    pack_file_max_mb: float = key(1024.0, serves="SEC-13", layers=ENGINEERING, unit="MB", ge=1, le=16384)
    pack_diff_budget_mb: float = key(10.0, serves="CUR-34", layers=ENGINEERING, unit="MB", ge=0.1, le=1024)
    # --- content updates download in chunks with range resume (PLT-11) ---
    download_chunk_mb: int = key(8, serves="PLT-11", layers=ENGINEERING, unit="MB", ge=1, le=256, desktop_shell=True)
    download_request_timeout_s: float = key(
        30.0, serves="PLT-11", layers=ENGINEERING, unit="s", ge=1, le=300, desktop_shell=True
    )
    download_control_poll_ms: int = key(
        100, serves="PLT-11", layers=ENGINEERING, unit="ms", ge=10, le=1000, desktop_shell=True
    )
    download_policy_poll_ms: int = key(
        500, serves="ARC-11", layers=ENGINEERING, unit="ms", ge=50, le=5000, desktop_shell=True
    )
    net_os_hint_poll_ms: int = key(
        1000, serves="ARC-08", layers=ENGINEERING, unit="ms", ge=100, le=60000, desktop_shell=True
    )
    download_metered_large_mb: int = key(
        50, serves="ARC-08", layers=ENGINEERING, unit="MB", ge=1, le=4096, desktop_shell=True
    )
    download_redirect_max: int = key(
        5, serves="ARC-06", layers=ENGINEERING, unit="redirects", ge=0, le=10, desktop_shell=True
    )
    # --- M20 packaging, packs, updates and crash capture (proposals from PLT-02, PLT-10 to PLT-16, PLT-23, OBS-16,
    # OBS-18, TABLE OPS-08a) ---
    download_chunk_retries: int = key(3, serves="PLT-11", layers=ENGINEERING, unit="retries", ge=1, le=20,
                                      desktop_shell=True)  # fmt: skip
    download_disk_margin_share: float = key(0.10, serves="PLT-11", layers=ENGINEERING, unit="share", ge=0, le=1,
                                            desktop_shell=True)  # fmt: skip
    download_pause_free_mb: float = key(500.0, serves="PLT-23", layers=ENGINEERING, unit="MB", ge=50, le=100_000,
                                        desktop_shell=True)  # fmt: skip
    update_check_interval_h: float = key(24.0, serves="PLT-14", layers=ENGINEERING, unit="h", ge=1, le=720,
                                         desktop_shell=True)  # fmt: skip
    app_update_budget_mb: float = key(150.0, serves="PLT-14", layers=ENGINEERING, unit="MB", ge=1, le=4096)
    memory_headroom_gb: float = key(2.0, serves="PLT-23", layers=ENGINEERING, unit="GB", ge=0, le=64)
    app_with_three_packs_budget_mb: float = key(1024.0, serves="PLT-23", layers=ENGINEERING, unit="MB", ge=1)
    model_pack_min_params_b: float = key(7.0, serves="PLT-10", layers=ENGINEERING, unit="B parameters", ge=1)
    usb_install_budget_min: float = key(10.0, serves="PLT-12", layers=ENGINEERING, unit="min", ge=1, le=240)
    bundle_budget_shell_mb: float = key(25.0, serves="PLT-02", layers=ENGINEERING, unit="MB", ge=1)
    bundle_budget_agent_core_mb: float = key(90.0, serves="PLT-02", layers=ENGINEERING, unit="MB", ge=1)
    bundle_budget_tool_worker_mb: float = key(80.0, serves="PLT-02", layers=ENGINEERING, unit="MB", ge=1)
    bundle_budget_runtime_mb: float = key(40.0, serves="PLT-02", layers=ENGINEERING, unit="MB", ge=1)
    bundle_budget_config_mb: float = key(5.0, serves="PLT-02", layers=ENGINEERING, unit="MB", ge=0.1)
    bundle_budget_keys_mb: float = key(1.0, serves="PLT-02", layers=ENGINEERING, unit="MB", ge=0.01)
    bundle_budget_total_mb: float = key(250.0, serves="PLT-02", layers=ENGINEERING, unit="MB", ge=1)
    bundle_budget_tolerance: float = key(0.10, serves="PLT-02", layers=ENGINEERING, unit="share", ge=0, le=1)
    # How long the release check waits for each of macOS's `stapler validate` and `spctl --assess` before it fails
    # the Gatekeeper check instead of hanging the suite.
    # CUR-36, ASM-23: a correction's re-mark that fails is tried again only after a back-off that doubles from the
    # first delay up to the longest (not at every start and turn boundary), and is named in health from this many
    # failures in a row.
    remark_retry_first_delay_s: float = key(900.0, serves="CUR-36", layers=ENGINEERING, unit="s", ge=1, le=86_400)
    remark_retry_max_delay_s: float = key(86_400.0, serves="CUR-36", layers=ENGINEERING, unit="s", ge=60,
                                          le=30 * 86_400)  # fmt: skip
    remark_failures_reported: int = key(2, serves="CUR-36", layers=ENGINEERING, unit="failures", ge=1, le=100)
    gatekeeper_check_timeout_s: float = key(120.0, serves="PLT-05", layers=ENGINEERING, unit="s", ge=1, le=1800)
    # How long the offline update signing waits for the updater key's signing command on one bundle (it may wait for
    # the key's password at the ceremony laptop's keyboard).
    updater_sign_command_timeout_s: float = key(600.0, serves="SEC-12", layers=ENGINEERING, unit="s", ge=10, le=7200)
    rollout_steps: tuple[float, ...] = key((0.10, 0.50, 1.0), serves="PLT-16", layers=ENGINEERING,
                                           unit="share of online devices per step")  # fmt: skip
    rollout_days: float = key(7.0, serves="PLT-16", layers=ENGINEERING, unit="days", ge=1, le=90)
    rollout_crash_free_gate: float = key(0.995, serves="PLT-16", layers=ENGINEERING, unit="share", ge=0.5, le=1)
    crash_stderr_tail_lines: int = key(200, serves="OBS-16", layers=ENGINEERING, unit="lines", ge=10, le=10_000,
                                       desktop_shell=True)  # fmt: skip
    debug_bundle_log_days: float = key(3.0, serves="OBS-18", layers=ENGINEERING, unit="days", ge=1, le=30)
    debug_bundle_trace_turns: int = key(50, serves="OBS-18", layers=ENGINEERING, unit="turns", ge=1, le=1000)
    # --- signing key rotation (SEC-12, TABLE SEC-12a): the longest validity a certificate may be issued with ---
    signing_root_rotation_years: int = key(5, serves="SEC-12", layers=ENGINEERING, unit="years", ge=1, le=20)
    signing_content_rotation_years: int = key(1, serves="SEC-12", layers=ENGINEERING, unit="years", ge=1, le=10)
    signing_model_rotation_years: int = key(1, serves="SEC-12", layers=ENGINEERING, unit="years", ge=1, le=10)
    signing_agent_bundle_rotation_years: int = key(1, serves="SEC-12", layers=ENGINEERING, unit="years", ge=1, le=10)
    signing_updater_rotation_years: int = key(1, serves="SEC-12", layers=ENGINEERING, unit="years", ge=1, le=10)
    signing_licence_rotation_years: int = key(2, serves="SEC-12", layers=ENGINEERING, unit="years", ge=1, le=10)
    # --- evaluation harness (EVL-01, EVL-11, EVL-14, EVL-15, EVL-18); read by the harness, never by a student path ---
    eval_oracle_items_per_subject_level: int = key(300, serves="EVL-01", layers=ENGINEERING, unit="items", ge=1)
    eval_safety_items_per_category: int = key(40, serves="EVL-11", layers=ENGINEERING, unit="items", ge=1)
    # The held-out split of EVL-18 as CUR-22 states it (30% of each suite, stratified by KC); one key serves both.
    eval_held_out_share: float = key(0.3, serves="CUR-22", layers=ENGINEERING, unit="share", ge=0.05, le=0.5)
    eval_held_out_runs_per_candidate: int = key(1, serves="EVL-18", layers=ENGINEERING, unit="runs", ge=1, le=3)
    devlab_min_laptop_ram_gb: int = key(8, serves="EVL-14", layers=ENGINEERING, unit="GB", ge=4, le=32)
    devlab_min_laptop_cpu_year: int = key(2018, serves="EVL-14", layers=ENGINEERING, unit="year", ge=2010, le=2030)
    devlab_school_laptops: int = key(5, serves="EVL-14", layers=ENGINEERING, unit="laptops", ge=0)
    devlab_timed_runs: int = key(20, serves="EVL-14", layers=ENGINEERING, unit="runs", ge=5, le=200)
    devlab_warmup_runs: int = key(2, serves="EVL-14", layers=ENGINEERING, unit="runs", ge=0, le=20)
    devlab_ram_headroom_gb: float = key(2.0, serves="EVL-14", layers=ENGINEERING, unit="GB", ge=0, le=16)
    # --- M14.j: simulated students, the pedagogy rubric and judge calibration (EVL-04 to EVL-06; proposals) ---
    eval_misconception_named_rate: float = key(0.80, serves="EVL-04", layers=ENGINEERING, unit="share", ge=0.5, le=1)
    eval_simulated_withholding_rate: float = key(0.95, serves="EVL-04", layers=ENGINEERING, unit="share", ge=0.5,
                                                 le=1)  # fmt: skip
    eval_persona_review_sample: int = key(50, serves="EVL-04", layers=ENGINEERING, unit="runs", ge=1, le=1000)
    eval_rubric_criterion_gate: float = key(0.90, serves="EVL-05", layers=ENGINEERING, unit="share", ge=0.5, le=1)
    eval_judge_calibration_excerpts: int = key(200, serves="EVL-06", layers=ENGINEERING, unit="excerpts", ge=10)
    eval_judge_min_agreement: float = key(0.85, serves="EVL-06", layers=ENGINEERING, unit="share", ge=0.5, le=1)
    coach_first_text_budget_s: tuple[float, ...] = key(
        (5.0, 3.0, 2.0), serves="AGT-28", layers=ENGINEERING, unit="s (low, mid, high), median after every gate"
    )
    restatement_budget_s: float = key(1.0, serves="AGT-28", layers=ENGINEERING, unit="s", ge=0.1, le=10)
    guided_step_budget_s: tuple[float, ...] = key(
        (30.0, 20.0, 10.0), serves="AGT-28", layers=ENGINEERING, unit="s (low, mid, high), median"
    )
    # --- authoring audit log retention after the last pack containing the record (AUT-02) ---
    audit_retention_years: int = key(5, serves="AUT-02", layers=ENGINEERING, unit="years", ge=1, le=50)
    # --- release gates (TABLE EVL-17a, S1 column) and stage exit metrics (TABLE PRD-17a, S1 rows) ---
    gate_final_answer_correct: float = key(0.90, serves="EVL-17", layers=ENGINEERING, unit="share", ge=0.5, le=1)
    gate_step_first_wrong_found: float = key(0.95, serves="EVL-17", layers=ENGINEERING, unit="share", ge=0.5, le=1)
    gate_step_category_matches: float = key(0.80, serves="EVL-17", layers=ENGINEERING, unit="share", ge=0.5, le=1)
    gate_structured_first_pass: float = key(0.99, serves="EVL-17", layers=ENGINEERING, unit="share", ge=0.5, le=1)
    gate_judged_withholding: float = key(0.95, serves="PED-08", layers=ENGINEERING, unit="share", ge=0.5, le=1)
    gate_held_out_max_drop_points: float = key(2.0, serves="EVL-17", layers=ENGINEERING, unit="points", ge=0, le=20)
    gate_topic_top1: float = key(0.90, serves="EVL-13", layers=ENGINEERING, unit="share", ge=0.5, le=1)  # TABLE EVL-17a
    # The memory suite's gates (M15 design §5).
    gate_memory_recall: float = key(0.90, serves="EVL-17", layers=ENGINEERING, unit="share", ge=0.5, le=1)
    gate_note_support: float = key(0.95, serves="MEM-23", layers=ENGINEERING, unit="share", ge=0.5, le=1)
    gate_note_capture_violations: int = key(0, serves="EVL-17", layers=ENGINEERING, unit="notes", ge=0, le=100)
    gate_fake_saves: int = key(0, serves="MEM-23", layers=ENGINEERING, unit="turns", ge=0, le=100)
    # PLT-22: low-power mode below this battery level (proposal), the Coach's output cap while saving (an engineering
    # value: the specification says only "shorter outputs"), how often the battery is read, and the battery budgets
    # per class (TABLE PLT-22a, content-only, low, mid, high; proposals until EVL-14 measures them).
    low_power_battery_pct: float = key(30.0, serves="PLT-22", layers=ENGINEERING, unit="%", ge=1, le=100)
    low_power_max_output_tokens: int = key(384, serves="PLT-22", layers=ENGINEERING, unit="tokens", ge=64, le=8192)
    low_power_battery_poll_s: float = key(60.0, serves="PLT-22", layers=ENGINEERING, unit="s", ge=1, le=3600)
    battery_budget_normal_w: tuple[float, ...] = key(
        (8.0, 15.0, 18.0, 25.0), serves="PLT-22", layers=ENGINEERING, unit="W (content-only, low, mid, high)"
    )
    battery_budget_low_power_w: tuple[float, ...] = key(
        (8.0, 10.0, 12.0, 17.0), serves="PLT-22", layers=ENGINEERING, unit="W (content-only, low, mid, high)"
    )
    low_power_min_saving: float = key(0.30, serves="PLT-22", layers=ENGINEERING, unit="share", ge=0, le=1)
    # ARC-32: a background job is cancelled within this when a turn starts (proposal), and waits this long between
    # looks while held.
    background_cancel_budget_s: float = key(1.0, serves="ARC-32", layers=ENGINEERING, unit="s", ge=0.05, le=30)
    # OBS-24: what tracing, logging and metrics together may add on the low-class reference laptop (proposals).
    obs24_max_turn_share: float = key(0.02, serves="OBS-24", layers=ENGINEERING, unit="share", ge=0, le=1)
    obs24_max_rss_mb: float = key(30.0, serves="OBS-24", layers=ENGINEERING, unit="MB", ge=0, le=4096)
    # EVL-12: the printed-photo set's size and its gate per hardware class (proposals; exit evidence of M11).
    evl12_printed_set_size: int = key(200, serves="EVL-12", layers=ENGINEERING, unit="photos", ge=1, le=10_000)
    evl12_printed_gate: float = key(0.90, serves="EVL-12", layers=ENGINEERING, unit="share", ge=0.5, le=1)
    exit_s1_first_text_median_s: float = key(3.0, serves="PRD-17", layers=ENGINEERING, unit="s", ge=0.1, le=60)
    exit_s1_restatement_median_s: float = key(1.0, serves="PRD-17", layers=ENGINEERING, unit="s", ge=0.1, le=10)
    # TABLE PRD-17a, S2 rows (proposals): the release-gate rows measured by answer oracles and simulated students, and
    # the pilot rows reported at the end of the S2 pilot (PRD-11 lets a closed pilot ship before those are met).
    exit_s2_final_answer_correct: float = key(0.95, serves="PRD-17", layers=ENGINEERING, unit="share", ge=0.5, le=1)
    exit_s2_guided_withholding: float = key(0.95, serves="PRD-17", layers=ENGINEERING, unit="share", ge=0.5, le=1)
    exit_s2_crash_free_sessions: float = key(0.995, serves="PRD-17", layers=ENGINEERING, unit="share", ge=0.5, le=1)
    exit_s2_weekly_active_share: float = key(0.60, serves="PRD-17", layers=ENGINEERING, unit="share", ge=0, le=1)
    exit_s2_median_active_days: float = key(3.0, serves="PRD-17", layers=ENGINEERING, unit="days/week", ge=0, le=7)
    exit_s2_learning_gain_sd: float = key(0.2, serves="PRD-17", layers=ENGINEERING, unit="SD", ge=0, le=5)
    exit_s2_task_success: float = key(0.80, serves="PRD-17, JRN-02", layers=ENGINEERING, unit="share", ge=0, le=1)
    # TABLE PRD-17a, S3 rows (proposals): A level answers and Biology facts on the release reports, and the voice and
    # read-aloud latencies measured on the reference laptops (TABLE VOC-13a, VOC-18).
    exit_s3_a_level_final_answer: float = key(0.95, serves="PRD-17", layers=ENGINEERING, unit="share", ge=0.5, le=1)
    exit_s3_biology_unsupported_max: float = key(0.02, serves="PRD-17", layers=ENGINEERING, unit="share", ge=0, le=1)
    exit_s3_voice_first_audio_p50_s: float = key(3.5, serves="PRD-17", layers=ENGINEERING, unit="s", ge=0.1, le=60)
    exit_s3_voice_first_audio_p90_s: float = key(6.3, serves="PRD-17", layers=ENGINEERING, unit="s", ge=0.1, le=60)
    exit_s3_read_aloud_p50_s: float = key(1.0, serves="PRD-17", layers=ENGINEERING, unit="s", ge=0.1, le=10)
    exit_s3_read_aloud_p90_s: float = key(1.6, serves="PRD-17", layers=ENGINEERING, unit="s", ge=0.1, le=10)
    # UXS-14 (JRN-04 step 4): the restated question, the knowns panel and the topic's visual reach the board this soon.
    board_ready_ms: int = key(1000, serves="UXS-14", layers=ENGINEERING, unit="ms", ge=50, le=10_000)
    # EVL-21: a release report's teacher review per shipped subject is at most this old (proposal: 14 days).
    teacher_review_max_age_days: int = key(14, serves="EVL-21", layers=ENGINEERING, unit="days", ge=1, le=365)
    # EVL-21: the weekly sample per subject (proposals): dialogues and conceptual answers, plus every failing item.
    teacher_review_dialogues_min: int = key(30, serves="EVL-21", layers=ENGINEERING, unit="dialogues", ge=1)
    teacher_review_conceptual_min: int = key(30, serves="EVL-21", layers=ENGINEERING, unit="answers", ge=1)
    # VOC-21 voice gates (proposals), checked before `voice_launch`: word error rate on subject vocabulary, false
    # cut-offs, and listener-panel intelligibility per offered voice (raw LaTeX and number mismatches are always 0).
    voice_gate_wer_max: float = key(0.12, serves="VOC-21", layers=ENGINEERING, unit="share", ge=0, le=1)
    voice_gate_false_cutoff_max: float = key(0.05, serves="VOC-21", layers=ENGINEERING, unit="share", ge=0, le=1)
    voice_gate_intelligibility_min: float = key(4.0, serves="VOC-21", layers=ENGINEERING, unit="of 5", ge=1, le=5)
    # The latency rows of the voice gates (TABLE VOC-13a hands-free turn to first accepted audio on the mid class;
    # VOC-18 read-aloud to first audio on the low class; proposals, also TABLE PRD-17a S3 rows).
    voice_gate_turn_latency_s: tuple[float, float] = key(
        (3.5, 6.3), serves="VOC-13, VOC-21", layers=ENGINEERING, unit="s (p50, p90)"
    )
    voice_gate_read_aloud_latency_s: tuple[float, float] = key(
        (1.0, 1.6), serves="VOC-18, VOC-21", layers=ENGINEERING, unit="s (p50, p90)"
    )
    bakeoff_thinking_budget_high: int = key(1000, serves="EVL-15", layers=ENGINEERING, unit="tokens", ge=0, le=8192)
    bakeoff_weights: tuple[float, ...] = key(
        (0.30, 0.25, 0.15, 0.15, 0.10, 0.05),
        serves="EVL-15",
        layers=ENGINEERING,
        unit="weights: correctness, pedagogy, schema-valid first try, latency, perception, memory and battery",
    )

    telemetry_flush_interval_s: float = key(60.0, serves="OBS-20", layers=ENGINEERING, unit="s", ge=1)
    product_bottom_out_share_max: float = key(
        -1.0, serves="PRD-18", layers=ENGINEERING, unit="share; negative means no Product lead policy", ge=-1, le=1
    )
    # M23 policy thresholds (OBS-20, PRD-19, PRV-21, PRV-23, SEC-25, BIZ-13, EVL-22, TRN-02).
    telemetry_histogram_bounds: tuple[float, ...] = key(
        (0.0, 1.0, 10.0, 100.0, 1000.0, 10000.0, 100000.0), serves="OBS-13", layers=ENGINEERING
    )
    research_min_participants: int = key(10, serves="PRD-19", layers=ENGINEERING, unit="participants", ge=2)
    # PRV-23 owns the 60 days; the OPS-06 gate row "Red-team report within 60 days" reads the same key (D92).
    redteam_max_age_days: int = key(60, serves="PRV-23, OPS-06", layers=ENGINEERING, unit="days", ge=1)
    incident_triage_hours: float = key(24.0, serves="PRV-21", layers=ENGINEERING, unit="hours", ge=0)
    incident_review_workdays: int = key(10, serves="PRV-21", layers=ENGINEERING, unit="working days", ge=1)
    security_fuzz_hours_per_target: float = key(1.0, serves="SEC-25", layers=ENGINEERING, unit="CPU hours", ge=0)
    disclosure_triage_workdays: int = key(3, serves="SEC-26", layers=ENGINEERING, unit="working days", ge=1)
    security_critical_fix_days: int = key(14, serves="SEC-26", layers=ENGINEERING, unit="days", ge=1)
    wtp_parent_min: int = key(40, serves="BIZ-13", layers=ENGINEERING, unit="participants", ge=1)
    wtp_school_owner_min: int = key(15, serves="BIZ-13", layers=ENGINEERING, unit="participants", ge=1)
    pilot_min_weeks: int = key(8, serves="EVL-22", layers=ENGINEERING, unit="weeks", ge=1)
    pilot_max_weeks: int = key(12, serves="EVL-22", layers=ENGINEERING, unit="weeks", ge=1)
    pilot_min_effect_sd: float = key(0.2, serves="EVL-22", layers=ENGINEERING, unit="SD", ge=0)
    pilot_export_retention_months: int = key(12, serves="BIZ-16", layers=ENGINEERING, unit="months", ge=1)
    training_gap_margin_points: float = key(2.0, serves="TRN-02", layers=ENGINEERING, unit="points", ge=0)
    pilot_country_file: str = key("", serves="PRD-22", layers=ENGINEERING)
    # M33.b: item analytics and calibration in the authoring system (CUR-26, ASM-21, AUT-37), from evaluation runs and
    # separately consented research exports only (D51). They flag records for review and never edit one.
    item_facility_low: float = key(0.10, serves="CUR-26, ASM-21", layers=ENGINEERING, unit="share", ge=0, le=1)
    item_facility_high: float = key(0.95, serves="CUR-26, ASM-21", layers=ENGINEERING, unit="share", ge=0, le=1)
    item_discrimination_min: float = key(0.1, serves="CUR-26, ASM-21", layers=ENGINEERING, unit="point-biserial",
                                         ge=-1, le=1)  # fmt: skip
    item_distractor_min_share: float = key(0.02, serves="CUR-26", layers=ENGINEERING, unit="share", ge=0, le=1)
    item_analytics_min_responses: int = key(200, serves="CUR-26", layers=ENGINEERING, unit="responses", ge=1)
    item_time_kc_multiple: float = key(3.0, serves="ASM-21", layers=ENGINEERING, unit="multiple of KC median",
                                       ge=1, le=20)  # fmt: skip
    calibration_min_responses: int = key(100, serves="ASM-21", layers=ENGINEERING, unit="responses", ge=1)
    # Engineering: the spread of the 1PL fit's normal prior around the author's rating (its shrinkage).
    calibration_prior_sd: float = key(1.0, serves="ASM-21", layers=ENGINEERING, unit="logit", ge=0.05, le=10)
    author_analytics_min_students: int = key(10, serves="AUT-37", layers=ENGINEERING, unit="students", ge=2)
    # LRN-12: per-student calibration on device, offline, in idle time.
    personal_rate_min_observations: int = key(200, serves="LRN-12", layers=ENGINEERING, unit="observations", ge=1)
    personal_fsrs_min_reviews: int = key(400, serves="LRN-12", layers=ENGINEERING, unit="reviews", ge=1)
    personal_fit_min_gain: float = key(0.02, serves="LRN-12", layers=ENGINEERING, unit="share of log loss", ge=0,
                                       le=1)  # fmt: skip
    personal_fit_recent_share: float = key(0.2, serves="LRN-12", layers=ENGINEERING, unit="share of events", ge=0.05,
                                           le=0.5)  # fmt: skip
    # Engineering: the spread of the normal prior on log(multiplier) around 1.0 (the fit's shrinkage).
    personal_rate_prior_sd: float = key(0.5, serves="LRN-12", layers=ENGINEERING, unit="log multiplier", ge=0.01,
                                        le=5)  # fmt: skip
    # M24: OPS-02..38 policy defaults.
    ops_daily_check_max_count: int = key(1, serves="OPS-16", layers=ENGINEERING, unit="checks/day", ge=1, le=1)
    ops_switch_response_s: float = key(1.0, serves="OPS-12", layers=ENGINEERING, unit="s", ge=0.001)
    ops_rollout_poll_s: int = key(60, serves="OPS-09", layers=ENGINEERING, unit="s", ge=1)
    ops_metadata_retention_days: int = key(30, serves="OPS-13, OPS-19", layers=ENGINEERING, unit="days", ge=1)
    ops_model_format_major: int = key(1, serves="OPS-02", layers=ENGINEERING, ge=1)
    ops_support_contacts: tuple[str, ...] = key((), serves="OPS-23", layers=ENGINEERING)
    ops_support_months: int = key(18, serves="OPS-03", layers=ENGINEERING, unit="months", ge=1)
    ops_newer_minors: int = key(2, serves="OPS-03", layers=ENGINEERING, unit="releases", ge=1)
    ops_newer_stable_months: int = key(6, serves="OPS-03", layers=ENGINEERING, unit="months", ge=1)
    ops_student_note_lines: int = key(3, serves="OPS-37", layers=ENGINEERING, unit="lines", ge=1)
    ops_r0_days: float = key(3.0, serves="OPS-08", layers=ENGINEERING, unit="days", ge=0.000001)
    ops_r1_days: float = key(7.0, serves="OPS-08", layers=ENGINEERING, unit="days", ge=0.000001)
    ops_r3_days: float = key(7.0, serves="OPS-08", layers=ENGINEERING, unit="days", ge=0.000001)
    ops_pilot_school_days: int = key(3, serves="OPS-08", layers=ENGINEERING, unit="days", ge=1)
    ops_hotfix_soak_hours: float = key(24.0, serves="OPS-08", layers=ENGINEERING, unit="hours", ge=0.000001)
    ops_update_failure_limit: float = key(0.01, serves="OPS-08", layers=ENGINEERING, unit="share", ge=0.000001)
    ops_rollback_limit: float = key(0.005, serves="OPS-08", layers=ENGINEERING, unit="share", ge=0.000001)
    ops_champion_min: float = key(0.8, serves="OPS-08", layers=ENGINEERING, unit="share", ge=0.000001)
    ops_halt_deadline_s: int = key(3600, serves="OPS-09", layers=ENGINEERING, unit="s", ge=1)
    ops_usb_sample_every: int = key(50, serves="OPS-15", layers=ENGINEERING, unit="kits", ge=1)
    ops_usb_min_samples: int = key(3, serves="OPS-15", layers=ENGINEERING, unit="kits", ge=1)
    ops_update_kit_max_bytes: int = key(2000000000, serves="OPS-15", layers=ENGINEERING, unit="bytes", ge=1)
    ops_emergency_kit_hours: int = key(48, serves="OPS-15", layers=ENGINEERING, unit="hours", ge=1)
    ops_daily_check_bytes: int = key(20000, serves="OPS-16", layers=ENGINEERING, unit="bytes", ge=1)
    ops_app_patch_bytes: int = key(30000000, serves="OPS-16", layers=ENGINEERING, unit="bytes", ge=1)
    ops_app_minor_bytes: int = key(150000000, serves="OPS-16", layers=ENGINEERING, unit="bytes", ge=1)
    ops_content_diff_bytes: int = key(10000000, serves="OPS-16", layers=ENGINEERING, unit="bytes", ge=1)
    ops_daily_telemetry_bytes: int = key(20000, serves="OPS-16", layers=ENGINEERING, unit="bytes", ge=1)
    ops_crash_bytes: int = key(200000, serves="OPS-16", layers=ENGINEERING, unit="bytes", ge=1)
    ops_metered_month_bytes: int = key(100000000, serves="OPS-16", layers=ENGINEERING, unit="bytes", ge=1)
    ops_alert_delay_s: int = key(300, serves="OPS-17", layers=ENGINEERING, unit="s", ge=1)
    ops_error_budget_pause_share: float = key(0.5, serves="OPS-17", layers=ENGINEERING, unit="share", ge=0.000001)
    ops_first_words_median_s: float = key(2.0, serves="OPS-17", layers=ENGINEERING, unit="s", ge=0.000001)
    ops_first_words_p95_s: float = key(4.0, serves="OPS-17", layers=ENGINEERING, unit="s", ge=0.000001)
    ops_percentile: float = key(0.95, serves="OPS-17", layers=ENGINEERING, unit="share", ge=0.000001)
    ops_index_availability: float = key(0.999, serves="OPS-17", layers=ENGINEERING, unit="share", ge=0.000001)
    ops_standard_availability: float = key(0.99, serves="OPS-17", layers=ENGINEERING, unit="share", ge=0.000001)
    ops_gateway_availability: float = key(0.995, serves="OPS-17", layers=ENGINEERING, unit="share", ge=0.000001)
    ops_index_p95_s: float = key(1.0, serves="OPS-17", layers=ENGINEERING, unit="s", ge=0.000001)
    ops_checkout_p95_s: float = key(2.0, serves="OPS-17", layers=ENGINEERING, unit="s", ge=0.000001)
    ops_gateway_added_p95_s: float = key(0.3, serves="OPS-17", layers=ENGINEERING, unit="s", ge=0.000001)
    ops_peak_share: float = key(0.15, serves="OPS-18", layers=ENGINEERING, unit="share", ge=0.000001)
    ops_load_multiple: float = key(2.0, serves="OPS-18", layers=ENGINEERING, unit="multiple", ge=0.000001)
    ops_capacity_multiple: float = key(1.5, serves="OPS-18", layers=ENGINEERING, unit="multiple", ge=0.000001)
    ops_cost_multiple: float = key(3.0, serves="OPS-19", layers=ENGINEERING, unit="multiple", ge=0.000001)
    ops_cost_history_days: int = key(14, serves="OPS-19", layers=ENGINEERING, unit="days", ge=1)
    ops_price_review_workdays: int = key(5, serves="OPS-19", layers=ENGINEERING, unit="workdays", ge=1)
    ops_probe_interval_s: int = key(60, serves="OPS-20", layers=ENGINEERING, unit="s", ge=1)
    ops_probe_window_s: int = key(300, serves="OPS-20", layers=ENGINEERING, unit="s", ge=1)
    ops_probe_failure_share: float = key(0.05, serves="OPS-20", layers=ENGINEERING, unit="share", ge=0.000001)
    ops_probe_latency_multiple: float = key(2.0, serves="OPS-20", layers=ENGINEERING, unit="multiple", ge=0.000001)
    ops_failback_healthy_s: int = key(900, serves="OPS-20", layers=ENGINEERING, unit="s", ge=1)
    ops_switch_max_age_s: int = key(300, serves="OPS-12", layers=ENGINEERING, unit="s", ge=1)
    ops_bundle_retention_days: int = key(30, serves="OPS-24", layers=ENGINEERING, unit="days", ge=1)
    ops_min_help_topics: int = key(10, serves="OPS-23", layers=ENGINEERING, unit="topics", ge=1)
    ops_support_open_hour: int = key(8, serves="OPS-26", layers=ENGINEERING, unit="hour", ge=1)
    ops_support_close_hour: int = key(17, serves="OPS-26", layers=ENGINEERING, unit="hour", ge=1)
    ops_p1_response_hours: int = key(4, serves="OPS-26", layers=ENGINEERING, unit="hours", ge=1)
    ops_p1_workaround_days: int = key(1, serves="OPS-26", layers=ENGINEERING, unit="schooldays", ge=1)
    ops_p2_response_days: int = key(1, serves="OPS-26", layers=ENGINEERING, unit="schooldays", ge=1)
    ops_p2_workaround_days: int = key(5, serves="OPS-26", layers=ENGINEERING, unit="schooldays", ge=1)
    ops_p3_response_days: int = key(3, serves="OPS-26", layers=ENGINEERING, unit="schooldays", ge=1)
    ops_triage_school_days: int = key(1, serves="OPS-27", layers=ENGINEERING, unit="schooldays", ge=1)
    ops_withdraw_hours: int = key(24, serves="OPS-27", layers=ENGINEERING, unit="hours", ge=1)
    ops_correction_days: int = key(7, serves="OPS-27", layers=ENGINEERING, unit="days", ge=1)
    ops_sev1_ack_hours: int = key(1, serves="OPS-30", layers=ENGINEERING, unit="hours", ge=1)
    ops_sev1_contain_hours: int = key(24, serves="OPS-30", layers=ENGINEERING, unit="hours", ge=1)
    ops_sev2_ack_hours: int = key(4, serves="OPS-30", layers=ENGINEERING, unit="supporthours", ge=1)
    ops_sev2_contain_days: int = key(2, serves="OPS-30", layers=ENGINEERING, unit="schooldays", ge=1)
    ops_school_notice_hours: int = key(24, serves="OPS-32", layers=ENGINEERING, unit="hours", ge=1)
    ops_review_workdays: int = key(10, serves="OPS-33", layers=ENGINEERING, unit="workdays", ge=1)
    ops_drill_quarter_days: int = key(92, serves="OPS-34", layers=ENGINEERING, unit="days", ge=1)
    ops_drill_halfyear_days: int = key(183, serves="OPS-34", layers=ENGINEERING, unit="days", ge=1)
    ops_drill_year_days: int = key(366, serves="OPS-34", layers=ENGINEERING, unit="days", ge=1)
    ops_weekly_window_days: int = key(7, serves="OPS-38", layers=ENGINEERING, unit="days", ge=1)


def declaration(name: str) -> dict[str, Any]:
    field = Settings.model_fields[name]
    extra = field.json_schema_extra
    assert isinstance(extra, dict)
    return extra


def annotated(name: str) -> Any:
    field = Settings.model_fields[name]
    return Annotated[field.annotation, *field.metadata] if field.metadata else field.annotation


def bounds(name: str) -> str:
    """The allowed range or values of a key, for error messages."""
    field = Settings.model_fields[name]
    low = next((m.ge for m in field.metadata if hasattr(m, "ge")), None)
    high = next((m.le for m in field.metadata if hasattr(m, "le")), None)
    if field.metadata and any(hasattr(m, "pattern") for m in field.metadata):
        return "pattern " + str(next(m.pattern for m in field.metadata if hasattr(m, "pattern")))
    values = get_args(getattr(field.annotation, "__value__", field.annotation))
    if low is not None or high is not None:
        return f"{low if low is not None else '-inf'}..{high if high is not None else 'inf'}"
    if values and all(isinstance(v, str) for v in values):
        return " | ".join(values)
    return field.annotation.__name__ if isinstance(field.annotation, type) else str(field.annotation)
