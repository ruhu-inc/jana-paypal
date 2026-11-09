"""Gateway configuration: every key declared once with its default, unit and the requirement it serves.

Proposal values from the specification are defaults here and never literals in code. A deployment's TOML file is
merged over the packaged `defaults.toml`. Provider slots map to adapters and concrete model ids in config only, so
swapping the primary and fallback providers is a config change (MDL-26). Keys are never configuration values: a
provider names the secret it needs and the secret source supplies it (SEC-20)."""

from __future__ import annotations

import math
import tomllib
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

from cloud_gateway.operations import OperationsPolicy

DEFAULTS = Path(__file__).with_name("defaults.toml")
Environment = Literal["dev", "staging", "pilot", "production"]
AdapterKind = Literal["openai_compat", "meta", "gemini", "vertex"]
Effort = Literal["none", "low", "medium", "high"]
Capability = Literal["text", "structured_output", "tool_calls", "vision", "audio", "streaming"]
PRODUCT_AUDIENCES = frozenset({"school", "adult"})


def key(default: Any, *, serves: str, unit: str | None = None, ge: float | None = None, le: float | None = None) -> Any:
    """A declared key: its default, unit and the requirement it serves."""
    return Field(default, ge=ge, le=le, json_schema_extra={"serves": serves, "unit": unit})


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ServicePermission(_Strict):
    """Deployment's reference to permission for this whole product, distinct from learner consent or model quality.

    No declaration ships with the app. The operator must supply the externally reviewed arrangement and its expiry;
    an adult-only service permission cannot enable a provider for the mixed-age product.
    """

    reference: str = Field(min_length=1, max_length=300, pattern=r"\S")
    audiences: frozenset[Literal["school", "adult"]]
    expires_at: AwareDatetime

    def permits(self, now: float) -> bool:
        return PRODUCT_AUDIENCES <= self.audiences and now < self.expires_at.timestamp()


class SearchEndpoint(_Strict):
    """An operator-selected HTTPS JSON search service, priced per request independently of any language model."""

    adapter: Literal["json_search"] = "json_search"
    url: str = Field(pattern=r"^https://[^?#\s]+$")
    secret: str | None = None
    permission: ServicePermission | None = None
    name: str = Field(default="search-service", pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    price_usd_per_1000: float = Field(ge=0, allow_inf_nan=False)
    max_results: int = Field(default=10, ge=1, le=30)
    max_response_bytes: int = Field(default=1_000_000, ge=1024, le=5_000_000)

    @field_validator("url")
    @classmethod
    def _destination(cls, value: str) -> str:
        parts = urlsplit(value)
        if not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
            raise ValueError("search endpoint must be a fixed HTTPS URL without credentials or query parameters")
        return value


class SpeechSelection(_Strict):
    """Independently selected speech services. No speech model or service permission ships as selected."""

    recognizer: Literal["none", "meta", "google", "custom"] = "none"
    synthesizer: Literal["none", "kokoro", "google", "custom"] = "none"
    live: Literal["none", "google", "custom"] = "none"
    recognition_permission: ServicePermission | None = None
    synthesis_permission: ServicePermission | None = None
    live_permission: ServicePermission | None = None


class Thinking(_Strict):
    """How a model's reasoning effort is set: a named level or a token budget, per OpenAI-style effort."""

    kind: Literal["level", "budget"]
    none: str | int | None = None
    low: str | int | None = None
    medium: str | int | None = None
    high: str | int | None = None

    def value(self, effort: Effort) -> str | int | None:
        return {"none": self.none, "low": self.low, "medium": self.medium, "high": self.high}[effort]


class AskSearch(_Strict):
    """The search endpoint Ask anything reads (STU-20): Programmable Search style JSON. The deployment restricts it to
    the safety lead's allow-list as well; the gateway filters every result again."""

    url: str = Field(pattern=r"^https://")
    secret: str  # the API key's name in the secret source, never the key itself (SEC-20)


class PodcastSpeech(_Strict):
    """Podcast narration by cloud voice's own verbatim voice (`voice_tts`, Kokoro-82M; JM-D13), bounded on its own."""

    enabled: bool = False
    daily_characters: int = Field(default=20000, ge=1, le=100000)
    total_characters: int = Field(default=100000, ge=1, le=1000000)
    daily_requests: int = Field(default=100, ge=1, le=500)
    total_requests: int = Field(default=500, ge=1, le=2000)
    expires_at: AwareDatetime | None = None


class Sam(_Strict):
    """SAM 3.1 segmentation through Meta's Responses API (JM-D10, SPEC §5): the photo regions Muse Spark read become
    exact masks so Jana can point at the learner's own working. Priced per image, not per token, and counted against
    the shared inference budget like chat (§4.3). Images are capped separately (§8.3: about 300 over the event)."""

    enabled: bool = False
    permission: ServicePermission | None = None
    adapter: Literal["sam"] = "sam"  # the HTTP client factory picks the transport by adapter kind, as for providers
    base_url: str = Field(default="https://api.meta.ai/v1", pattern=r"^https://")
    secret: str = "meta_api_key"  # noqa: S105 -- the secret's name in the secret source, not a key
    model: str = "sam-3.1"
    timeout_s: float = key(30.0, serves="ARC-12", unit="s", ge=1, le=120)
    image_usd: float = key(0.0025, serves="BIZ-08", unit="USD/image", ge=0)
    max_prompts: int = key(16, serves="MDL-26", unit="objects", ge=1, le=16)  # the model's documented limit
    daily_images: int = key(100, serves="SEC-20", unit="images", ge=1)
    total_images: int = key(300, serves="SEC-20", unit="images", ge=1)
    max_pixels: int = key(16_000_000, serves="SEC-20", unit="pixels", ge=1)


class Segmentation(_Strict):
    """Provider-neutral image segmentation over a reviewed deployment endpoint; masks use the existing wire shape."""

    enabled: bool = False
    adapter: Literal["json_segment"] = "json_segment"
    base_url: str = ""
    secret: str | None = None
    permission: ServicePermission | None = None
    provider: str = Field(default="segmentation-service", pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    model: str = ""
    image_usd: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    timeout_s: float = key(30.0, serves="ARC-12", unit="s", ge=1, le=120)
    max_prompts: int = key(16, serves="MDL-26", unit="objects", ge=1, le=16)
    max_pixels: int = key(16_000_000, serves="SEC-20", unit="pixels", ge=1)
    max_response_bytes: int = key(4_000_000, serves="SEC-20", unit="bytes", ge=1024)
    daily_images: int = key(100, serves="SEC-20", unit="images", ge=1)
    total_images: int = key(300, serves="SEC-20", unit="images", ge=1)

    @model_validator(mode="after")
    def _configured(self) -> Segmentation:
        if self.enabled:
            url = urlsplit(self.base_url)
            if (
                url.scheme != "https"
                or not url.hostname
                or url.username
                or url.password
                or url.query
                or url.fragment
                or not self.model.strip()
                or self.image_usd is None
            ):
                raise ValueError("segmentation needs a fixed HTTPS endpoint, explicit model and price")
        return self


class Research(_Strict):
    """The research agent's gateway job (SPEC §18.3-18.5, §18.10): one Responses API web search per sub-question on
    the Study model, then the result pages fetched and reduced to text. Searches are priced per 1,000 (they never
    appear in `usage`); each call holds its bound in the shared inference budget and the job's own cap. The domain
    lists are the source policy of §18.10: a host on `excluded_domains` is never fetched; `preferred_domains` and
    `preferred_suffixes` rank reference and educational sources first; a host under `excluded_suffixes` (top-level
    domains the live searches returned only spam from) is never fetched either. An entry may carry a path prefix
    (`bbc.co.uk/bitesize`)."""

    enabled: bool = False
    adapter: Literal["json_search", "meta_responses"] = "json_search"
    search: SearchEndpoint | None = None
    per_job_usd: float = key(0.30, serves="BIZ-08", unit="USD", ge=0)  # owner, 2026-10-26 (§18.10)
    daily_jobs_per_learner: int = key(5, serves="SEC-20", unit="jobs/day", ge=1)  # owner, 2026-10-26 (§18.10)
    max_searches: int = key(12, serves="BIZ-08", unit="searches", ge=1, le=64)
    max_pages: int = key(30, serves="STU-02", unit="pages", ge=3, le=30)  # the contract's bound on `pages`
    parallel: int = key(4, serves="ARC-12", unit="requests", ge=1, le=8)
    fetch_parallel: int = key(6, serves="ARC-12", unit="pages", ge=1, le=16)
    search_usd_per_1000: float = key(2.50, serves="BIZ-08", unit="USD/1,000 searches", ge=0)
    # Sent as `max_tool_calls`: left alone, Spark ran 9 searches for one broad question (43.7k input tokens, $0.068);
    # held to one it read 3.2k tokens for $0.0099 (live, 2026-10-27). §18.3: each sub-question is one search.
    searches_per_call: int = key(1, serves="BIZ-08", unit="searches", ge=1, le=8)
    # The hold per Responses call: Spark reads its search results as input tokens and reasons within the output, so
    # the input bound is generous; the call settles at its billed usage.
    search_input_tokens: int = key(16_000, serves="SEC-20", unit="tokens", ge=1000)
    search_output_tokens: int = key(2048, serves="SEC-20", unit="tokens", ge=256)
    search_timeout_s: float = key(90.0, serves="ARC-12", unit="s", ge=5, le=300)
    page_timeout_s: float = key(8.0, serves="SEC-31", unit="s", ge=1, le=30)
    page_max_bytes: int = key(1_000_000, serves="SEC-31", unit="bytes", ge=10_000, le=5_000_000)
    page_max_chars: int = key(60_000, serves="STU-02", unit="characters", ge=1000, le=60_000)
    page_min_chars: int = key(500, serves="STU-02", unit="characters", ge=1)
    max_redirects: int = key(3, serves="SEC-31", unit="hops", ge=0, le=5)
    job_timeout_s: float = key(900.0, serves="ARC-12", unit="s", ge=60)  # §18.4: hard stop at 15 minutes
    retention_s: float = key(86_400.0, serves="PRV-25", unit="s", ge=60, le=86_400)  # §18.6: at most 24 hours
    excluded_domains: tuple[str, ...] = ()
    excluded_suffixes: tuple[str, ...] = ()
    preferred_domains: tuple[str, ...] = ()
    preferred_suffixes: tuple[str, ...] = ()


class ModelSpec(_Strict):
    id: str
    thinking: Thinking | None = None
    capabilities: frozenset[Capability] = frozenset()
    context: int | None = Field(default=None, ge=1)
    max_output: int | None = Field(default=None, ge=1)


class ProviderSpec(_Strict):
    adapter: AdapterKind
    permission: ServicePermission | None = None
    base_url: str
    secret: str  # the secret's name in the secret source, never the key itself (SEC-20)
    timeout_s: float = key(60.0, serves="ARC-12", unit="s", ge=1)
    connect_timeout_s: float = key(5.0, serves="ARC-06", unit="s", ge=0.1)
    # The request field an OpenAI-compatible provider reads the session pseudonym from, if any (PRV-25).
    pseudonym_field: str | None = None
    # Study drafts display text as {prose, math} parts instead of LaTeX in strings, for a model that leaves raw
    # equations in prose. Off by default: Muse Spark writes delimited LaTeX (switch on if day-1 checks show otherwise).
    study_structured_math: bool = False
    models: dict[str, ModelSpec]  # model alias (fast-model, strong-model) -> concrete model


class Price(_Strict):
    """USD per million tokens (MDL-29, BIZ-08)."""

    input: float = Field(ge=0, allow_inf_nan=False)
    cached_input: float = Field(ge=0, allow_inf_nan=False)
    output: float = Field(ge=0, allow_inf_nan=False)


class GoogleVoice(_Strict):
    """Optional Google speech transports. The deployment supplies model/voice identifiers and prices;
    no speech service is selected by the product. Attached identity and whole-product permission remain separate.
    """

    project: str = Field(default="", pattern=r"^([a-z][a-z0-9-]{4,61}[a-z0-9])?$")
    # Regional streaming recognition; model availability is qualified separately.
    stt_model: str = ""
    # Enable only for a selected model/language qualified to accept phrase adaptation.
    stt_adaptation: bool = False
    stt_keywords: tuple[str, ...] | None = None
    stt_keywords_max: int = key(500, serves="VOC-28", unit="terms", ge=0, le=500)
    stt_location: Literal["us", "eu"] = "eu"
    # The recognizer locale for each transcription language the bridge sends (VOC-25). Nigerian English is not
    # offered (2026-10-21); add "en-NG" first in the list when it is.
    stt_locales: dict[str, list[str]] = Field(default_factory=lambda: {"en": ["en-GB"]})
    # The recognizer's end of speech (speech_end_timeout, at least 500 ms); the session's silence holds on from it.
    stt_end_timeout_ms: int = key(500, serves="VOC-28", unit="ms", ge=500, le=60_000)
    stt_minute_usd: float | None = key(None, serves="BIZ-08", unit="USD/minute", ge=0)
    # Gemini Live (native audio): "gemini" is the Gemini API with `live_secret`; "vertex" is Vertex AI with the
    # attached identity in `live_location`.
    live_api: Literal["gemini", "vertex"] = "vertex"
    live_model: str = ""
    live_voice: str = ""
    live_secret: str = "gemini_api_key"  # noqa: S105 -- the secret's name in the secret source, not a key
    live_location: str = Field(default="europe-west4", pattern=r"^[a-z]+-[a-z]+[0-9]$")
    # Cloud Text-to-Speech streaming synthesis, Chirp 3 HD (verbatim): the voice for each transcription language.
    tts_location: Literal["global", "us", "eu"] = "eu"
    tts_voices: dict[str, str] = Field(default_factory=dict)
    tts_million_chars_usd: float | None = key(None, serves="BIZ-08", unit="USD/1M characters", ge=0)


class MetaVoice(_Strict):
    """Muse Voice Transcribe, cloud voice's realtime speech to text (JM-D12, SPEC §6.1). Checked against
    dev.meta.ai/docs (api-reference/voice/realtime, speech-to-text, pricing-rate-limits) on 2026-10-27. The Meta Model
    API key named by `secret` goes in the stream's first frame only: the endpoint ignores the Authorization header and
    has no key parameter."""

    url: str = Field(default="wss://api.meta.ai/v1/asr/realtime", pattern=r"^wss://[^?#\s]+$")
    secret: str = "meta_api_key"  # noqa: S105 -- the secret's name in the secret source, not a key
    model: str = "muse-voice-transcribe-1.0"
    # The language names (not codes) biasing recognition for each transcription language the bridge sends. English
    # only (JM-D18).
    language_bias: dict[str, list[str]] = Field(default_factory=lambda: {"en": ["English"]})
    # Terms that bias recognition (they do not guarantee spelling). None is the packaged list made from the course
    # glossary (`voice_keywords.txt`). Meta refuses more than 500 (measured 2026-10-27: "Too many ASR keywords:
    # received 1368, maximum is 500", an `invalid_request_error`), so the list is trimmed to `keywords_max`.
    keywords: list[str] | None = None
    keywords_max: int = key(500, serves="VOC-28", unit="terms", ge=0, le=500)
    # The acknowledgement must follow the first frame; Meta closes a stream whose first frame is later than 10 s.
    handshake_timeout_s: float = key(5.0, serves="VOC-28", unit="s", ge=0.5, le=10)
    # Audio is paced at real time: at most this far ahead of the stream's clock (Meta allows 5 s), and a gap longer
    # than `pad_after_ms` in the device's audio is filled with silence (falling behind real time closes the stream).
    ahead_ms: int = key(4000, serves="VOC-28", unit="ms", ge=500, le=5000)
    pad_after_ms: int = key(1000, serves="VOC-28", unit="ms", ge=100, le=4000)
    # The recognizer's own end-of-speech silence in ENDPOINTING mode (not documented: measure); the session's
    # silence holds on from it.
    end_ms: int = key(500, serves="VOC-28", unit="ms", ge=0, le=10_000)
    # Voice Transcribe ends a turn about 0.6 s into a pause, which a pause between two sentences of one question can
    # reach (measured 2026-10-27: "... through a resistor. What is its resistance?" became two turns). Speech that
    # resumes within this hold of its end continues the utterance; it adds this much to every turn's transcript.
    min_hold_ms: int = key(600, serves="VOC-28", unit="ms", ge=0, le=5000)
    # $0.18 per hour of audio, billed in whole seconds (Meta rounds down; the ledger rounds up, per stream).
    hour_usd: float = key(0.18, serves="BIZ-08", unit="USD/audio hour", ge=0)


class VoiceTts(_Strict):
    """Cloud voice's verbatim voice (JM-D12, JM-D13, SPEC §6.4): Kokoro-82M (Apache-2.0, the speech pack's own voice,
    run here), so the audio anyone hears is licence-clean."""

    engine: Literal["kokoro"] = "kokoro"
    # Kokoro runs in a worker under the speech runtime's interpreter (torch, kokoro, misaki), from local files only.
    kokoro_python: str = "~/.cache/jana-speech/venv/bin/python"
    kokoro_model_dir: str = "~/.cache/jana-speech/packs/sp.en.0_1/models/kokoro-82m"
    kokoro_speaker: str = Field(default="af_heart", pattern=r"^[ab][fm]_[a-z]+$")
    kokoro_threads: int = key(0, serves="VOC-28", ge=0, le=64)
    # The first sentence is split once at its first clause when this many words come before it: first audio sooner.
    kokoro_first_clause_words: int = key(4, serves="VOC-28", unit="words", ge=0, le=40)
    kokoro_start_timeout_s: float = key(120.0, serves="VOC-28", unit="s", ge=5, le=600)
    # Engine-owned keyed phrases (no learner content: the VOC-12 filler) read once at startup and replayed, so the
    # filler's audio starts the moment it is accepted. Any other text is synthesised when it is given and not kept.
    fixed_phrases: tuple[str, ...] = ("Let me work that out.",)


class Quotas(_Strict):
    """Rate limits and spend caps. The subscription's own cap is its monthly allowance (`Tier`, BIZ-07, D33): school
    seats are set aside (D10, decided in D100), so nothing here is per school (MDL-29: no per-school budget). A
    licence is the one the subscription's entitlement names."""

    device_requests_per_minute: float = key(30.0, serves="SEC-20", unit="requests/min", ge=0)
    device_burst: float = key(10.0, serves="SEC-20", unit="requests", ge=1)
    licence_requests_per_minute: float = key(600.0, serves="SEC-20", unit="requests/min", ge=0)
    licence_burst: float = key(100.0, serves="SEC-20", unit="requests", ge=1)
    device_daily_spend_usd: float = key(2.0, serves="SEC-20", unit="USD", ge=0)
    # Optional deployment-wide Study ceiling, including unresolved provider reservations across all devices.
    study_daily_spend_usd: float | None = key(None, serves="SEC-20", unit="USD", ge=0)
    # Shared provider inference budget, persisted independently of metadata retention. Pending calls reserve their
    # full input/output bound before any provider send. Hosting charges are managed separately by the operator.
    deployment_daily_spend_usd: float | None = key(None, serves="SEC-20", unit="USD", ge=0)
    deployment_total_spend_usd: float | None = key(None, serves="SEC-20", unit="USD", ge=0)
    deployment_budget_expires_at: AwareDatetime | None = key(None, serves="SEC-20")
    deployment_max_output_tokens: int = key(8192, serves="SEC-20", unit="tokens", ge=1, le=65536)
    # The input-token bound reserved per image. Muse Spark bills images as input tokens, about 1,300-1,500 for a
    # ~1280 px image (dev.meta.ai/docs/image-understanding); the perception worker sends at most 1600 px.
    deployment_image_input_tokens: int = key(4096, serves="SEC-20", unit="tokens", ge=1, le=65536)
    # Spend anomaly: today's spend over this multiple of the trailing daily mean (per device within its licence,
    # and per licence), once it is above the floor.
    anomaly_multiple: float = key(5.0, serves="SEC-20", ge=1)
    anomaly_trailing_days: int = key(7, serves="SEC-20", unit="days", ge=1)
    anomaly_min_usd: float = key(0.5, serves="SEC-20", unit="USD", ge=0)


class Tier(_Strict):
    """The online tier's usage allowance per student per month (BIZ-07, BIZ-08, BIZ-18; D33 and D121). The prices are
    TABLE BIZ-18a's (`Plans`); a term is `prepaid_term_months` months prepaid; the allowance resets each month of the
    paid period. Every paid plan and band gives one allowance with no premium quota (D121): LIST BIZ-08a formula 5,
    `price · (1 - gross margin) / p_blend`, at the lowest price a covered student's month brings in across the table,
    so every plan covers its cost, with `p_blend` the price per token of this gateway's price table at the usage mix of
    TABLE BIZ-08b (`cloud_gateway.tier`). If BIZ-08 shows a plan cannot cover its cost, the allowance changes before
    the price does: `allowance_usd_per_student_month` sets it directly. The checkout signs the token figure into the
    entitlement; the gateway holds each subscription to the same allowance in US dollars."""

    prepaid_term_months: int = key(3, serves="BIZ-07", unit="months", ge=1)  # D33
    gross_margin: float = key(0.6, serves="BIZ-08", unit="share", ge=0, le=0.99)  # proposal (BIZ-08 formula 4)
    # Unset: computed from TABLE BIZ-18a's lowest price per student month. Set: the allowance, whatever the prices.
    allowance_usd_per_student_month: float | None = key(None, serves="BIZ-18", unit="USD", ge=0)
    # TABLE BIZ-08b's usage mix for `p_blend` (proposals until pilot traces replace them): model calls per tutor turn
    # by the primary provider's model alias (Coach, the rule matcher and Checker phrasing on the fast model), and
    # tokens per call with the cached share of the input.
    calls_per_turn: dict[str, float] = key({"fast-model": 3.0}, serves="BIZ-08", unit="calls")
    input_tokens_per_call: float = key(3000.0, serves="BIZ-08", unit="tokens", ge=0)
    output_tokens_per_call: float = key(250.0, serves="BIZ-08", unit="tokens", ge=0)
    cache_hit_share: float = key(0.6, serves="BIZ-08", unit="share", ge=0, le=1)


Band = Literal["nigeria", "purchasing_power", "international"]
Billing = Literal["month", "term", "year"]
PlanId = Literal["jana_online", "jana_family"]
Feature = Literal["cloud_tier", "ask_anything", "study_area", "uploads", "recordings", "podcasts", "past_paper_import",
                  "online_voice", "exam_reminders"]  # fmt: skip
BANDS: tuple[Band, ...] = ("nigeria", "purchasing_power", "international")


class Offer(_Strict):
    """One price in a band's currency, in whole units of it (TABLE BIZ-18a)."""

    currency: str = Field(pattern=r"^[A-Z]{3}$")
    amount: int = Field(ge=0)


# The `Limits` the gateway holds itself, never signed into the entitlement (its `PlanLimits` has no such key).
GATEWAY_HELD_LIMITS = frozenset({"companion_turns_per_day", "tutor_memory_jobs_per_day", "video_minutes_per_month"})


class Limits(_Strict):
    """A plan's fair-use limits per student (TABLE BIZ-18a, the entitlement's `PlanLimits`); unset means none. The
    personal learning agent's two daily caps (SPEC §19.4, §19.7) are held by the gateway itself, per session pseudonym
    per day (`cloud_gateway.companion`): the entitlement's `PlanLimits` does not carry them, so `granted` leaves them
    out of what the checkout signs."""

    ask_anything_per_day: int | None = Field(None, ge=0)
    uploads_per_day: int | None = Field(None, ge=0)
    uploads_per_week: int | None = Field(None, ge=0)
    upload_max_pages: int | None = Field(None, ge=0)
    upload_max_mb: int | None = Field(None, ge=0)
    recording_hours_per_month: int | None = Field(None, ge=0)
    podcasts_per_day: int | None = Field(None, ge=0)
    voice_minutes_per_month: int | None = Field(None, ge=0)
    companion_turns_per_day: int | None = Field(None, ge=0)  # the Study companion's turns (W8, SPEC §19.7)
    tutor_memory_jobs_per_day: int | None = Field(None, ge=0)  # the Reflector's jobs on Spark (W8, JM-D21)
    # Video minutes a month (W9, SPEC §20.3, JM-D44): drawn from the recording pool, one clock for one kind of
    # thing; unset, it is 60 times recording_hours_per_month. Held by the gateway, never signed into entitlements.
    video_minutes_per_month: int | None = Field(None, ge=0)

    @model_validator(mode="after")
    def _video_minutes_from_recording_hours(self) -> Limits:
        if self.video_minutes_per_month is None and self.recording_hours_per_month is not None:
            object.__setattr__(self, "video_minutes_per_month", 60 * self.recording_hours_per_month)
        return self

    def granted(self) -> dict[str, int]:
        """The limits the entitlement signs: every one set, except those the gateway holds itself."""
        return {name: value for name, value in self.model_dump().items()
                if value is not None and name not in GATEWAY_HELD_LIMITS}  # fmt: skip


class Taste(_Strict):
    """Free's online taste (TABLE BIZ-18a): the whole offline product needs nothing (BIZ-02, D84)."""

    features: tuple[Feature, ...]
    limits: Limits


class Plan(_Strict):
    name: str
    priced_per: Literal["student", "subscription"]  # Jana Online per student; Jana Family per subscription
    max_students: int = Field(ge=1, le=20)
    features: tuple[Feature, ...] = Field(min_length=1)
    limits: Limits
    prices: dict[Band, dict[Billing, Offer]]


class Plans(_Strict):
    """TABLE BIZ-18a (D121): the plans, their prices by band and what each gives, in one table (`plans.toml`). The
    band comes from the billing country at checkout, never from the device (BIZ-18). `per_usd` converts a band's
    currency for the allowance only (proposal rates); prices are never converted for the payer."""

    nigeria_countries: frozenset[str]
    purchasing_power_countries: frozenset[str]
    per_usd: dict[str, float]
    free: Taste
    paid: dict[PlanId, Plan]

    @model_validator(mode="after")
    def _consistent(self) -> Plans:
        if self.nigeria_countries & self.purchasing_power_countries:
            raise ValueError("a country is in two price bands (BIZ-18)")
        for country in self.nigeria_countries | self.purchasing_power_countries:
            if len(country) != 2 or not country.isupper():
                raise ValueError(f"{country!r} is not an ISO 3166 alpha-2 country code")
        for plan_id, plan in self.paid.items():
            for band in BANDS:
                if not plan.prices.get(band):
                    raise ValueError(f"{plan_id} has no price in the {band} band (TABLE BIZ-18a)")
            for offers in plan.prices.values():
                for offer in offers.values():
                    if self.per_usd.get(offer.currency, 0) <= 0:
                        raise ValueError(f"{offer.currency} has no rate to US dollars for the allowance")
        return self


PLANS = Path(__file__).with_name("plans.toml")


def packaged_plans() -> Plans:
    return Plans.model_validate(tomllib.loads(PLANS.read_text()))


class CompanionConfig(_Strict):
    """The personal learning agent's calls on the chat route (W8, SPEC §19.4, §19.7): the Study companion's turns and
    the Reflector's tutor-memory jobs. Each call holds its upper bound against this daily spend, per session pseudonym
    per day, before it is sent; the turn and job counts are the plan's `Limits`."""

    per_day_usd: float = key(0.50, serves="BIZ-08", unit="USD/day", ge=0)


class Objectives(_Strict):
    availability: float = key(0.995, serves="OBS-22", ge=0, le=1)  # proposal: monthly
    availability_window_days: int = key(30, serves="OBS-22", unit="days", ge=1)
    # Engineering assumption: the specification sets a latency objective without a number yet.
    latency_p95_ms: int = key(20000, serves="OBS-22", unit="ms", ge=1)
    status_window_s: int = key(300, serves="OBS-22", unit="s", ge=10)
    status_min_samples: int = key(5, serves="OBS-22", ge=1)
    degraded_failure_ratio: float = key(0.05, serves="OBS-22", ge=0, le=1)
    outage_failure_ratio: float = key(0.5, serves="OBS-22", ge=0, le=1)
    check_interval_s: int = key(60, serves="OBS-22", unit="s", ge=1)


class GatewayConfig(_Strict):
    environment: Environment = "production"
    # The host devices connect to; the token signature covers it so a signature is useless elsewhere (SEC-21).
    audience_host: str = "gateway.jana.invalid"
    storage_dir: Path = Path("/var/lib/jana-cloud-gateway")
    log_path: Path | None = None
    # Secret-manager mount: one file per secret, named after the secret (SEC-20).
    secrets_dir: Path | None = None
    keychain_service: str = "jana.cloud-gateway"

    @field_validator("storage_dir", "log_path", "secrets_dir", mode="after")
    @classmethod
    def _home(cls, value: Path | None) -> Path | None:
        """A leading `~` names the operator's home, so a dev ledger can live outside /tmp without a user name."""
        return value.expanduser() if value is not None else None

    # At most an hour each: the gateway contract's bound on `expires_in` (SEC-21, D39).
    access_token_ttl_s: int = key(900, serves="SEC-21", unit="s", ge=60, le=3600)  # proposal: 15 minutes
    challenge_ttl_s: int = key(60, serves="SEC-21", unit="s", ge=5, le=3600)
    # OBS-22's proposal. An allowance month can last 31 days, but the allowance is held by the month's own running
    # total (`GatewayStore.allowance_spend`, kept until its month is over), never by summing request rows, so this
    # window need not cover a month (D33).
    metadata_retention_days: int = key(30, serves="OBS-22", unit="days", ge=1)  # proposal
    max_request_bytes: int = key(8_000_000, serves="MDL-23", unit="bytes", ge=1024)
    # The unauthenticated routes (registration, challenge, token) take small bodies only (SEC-21).
    max_auth_request_bytes: int = key(16_384, serves="SEC-21", unit="bytes", ge=1024)
    # Registration needs the subscription entitlement the checkout signed (BIZ-07, D70, SEC-21): the licensing
    # service's licence intermediates that sign entitlements, by key id (base64 raw Ed25519 public keys; public
    # values, not secrets). When `licences` is not empty, the entitlement's licence must also be on it. Registration
    # without an entitlement (a bare licence id) is open only in a developer environment.
    entitlement_keys: dict[str, str] = Field(default_factory=dict)
    licences: frozenset[str] = frozenset()
    open_registration: bool = False
    # Devices each covered student of a subscription may hold live credentials for at once (so a Jana Family
    # subscription of four students may hold four times as many); the licence portal frees a slot by revoking.
    devices_per_subscription: int = key(2, serves="SEC-21", unit="devices", ge=1)
    slots: dict[str, str]  # provider slot (primary, fallback) -> provider name
    providers: dict[str, ProviderSpec]
    prices: dict[str, Price] = Field(default_factory=dict)  # concrete model id -> price
    quotas: Quotas = Field(default_factory=Quotas)
    tier: Tier = Field(default_factory=Tier)
    plans: Plans = Field(default_factory=packaged_plans)
    study_memory_bytes: int = key(256 * 1024 * 1024, serves="STU-17", unit="bytes", ge=104857600)
    study_source_max_mb: int = key(100, serves="STU-02", unit="MiB", ge=1, le=100)
    study_source_max_pages: int = key(1000, serves="STU-02", unit="pages", ge=1, le=1000)
    # Engineering proposal for gateway extraction compute; generation is metered separately by real token usage.
    study_extract_page_usd: float = key(0.0001, serves="STU-12", unit="USD/page", ge=0)
    study_extract_chars_per_page: int = key(4000, serves="STU-12", unit="characters/page", ge=1000, le=10000)
    study_recording_max_minutes: int = key(120, serves="STU-02", unit="minutes", ge=1, le=120)
    study_extract_minute_usd: float = key(0.0001, serves="STU-12", unit="USD/minute", ge=0)
    # The Study candidate is selected independently from the tutor's strong model.
    study_model: str = Field(default="primary/fast-model", pattern=r"^[a-z][a-z0-9_-]*/[a-z][a-z0-9_-]*$")
    # Trusted deployment command, including its OS sandbox wrapper. Never supplied by a device request.
    study_worker_command: tuple[str, ...] = ()
    # Cloud voice (VOC-28, D124): independently selected speech services behind our Realtime endpoint.
    # The tutor's words come
    # from the device's own engine through the connection, never from a model here. Audio and transcripts stay in
    # memory for the length of one utterance or one spoken reply.
    voice_enabled: bool = False
    # The voice service's own host name, accepted as a token audience beside `audience_host` (SEC-21).
    voice_audience_host: str | None = None
    voice_meta: MetaVoice = Field(default_factory=lambda: MetaVoice())
    voice_google: GoogleVoice = Field(default_factory=GoogleVoice)
    speech: SpeechSelection = Field(default_factory=lambda: SpeechSelection())
    voice_end_ms: int = key(500, serves="VOC-28", unit="ms", ge=0, le=10000)
    voice_min_hold_ms: int = key(600, serves="VOC-28", unit="ms", ge=0, le=5000)
    voice_tts: VoiceTts = Field(default_factory=lambda: VoiceTts())
    voice_max_utterance_s: float = key(30.0, serves="VOC-28", unit="s", ge=1, le=120)
    voice_max_session_s: float = key(3600.0, serves="VOC-28", unit="s", ge=60)
    # Prewarm (D124): `session.created`, which opens the device's microphone, waits this long for the recognition
    # stream and the Live session to open; audio sent before they are open is held, up to `voice_first_words_ms`.
    voice_prewarm_s: float = key(2.0, serves="VOC-28", unit="s", ge=0, le=10)
    voice_connect_timeout_s: float = key(5.0, serves="VOC-28", unit="s", ge=0.5, le=30)
    # Held audio reaches a new stream at once, so it stays within what Voice Transcribe takes ahead of real time.
    voice_first_words_ms: int = key(4000, serves="VOC-28", unit="ms", ge=1000, le=30_000)
    # Sound kept while the upstream sessions are released for idleness, sent first when a sound reopens them.
    voice_preroll_ms: int = key(500, serves="VOC-28", unit="ms", ge=100, le=2000)
    # Replace recognition streams at a quiet moment within the selected service's qualified limit.
    voice_stt_rotate_s: float = key(240.0, serves="VOC-28", unit="s", ge=30, le=3540)
    # After speech stops, how long the final transcript of the last words may take to arrive.
    voice_final_wait_ms: int = key(1500, serves="VOC-28", unit="ms", ge=100, le=10_000)
    # Adaptive idle timeout: factor x the longest quiet stretch seen before the student spoke, within these bounds;
    # suspended while a turn runs. On expiry the upstream sessions are released until the next sound.
    voice_idle_min_s: float = key(60.0, serves="VOC-28", unit="s", ge=1)
    voice_idle_max_s: float = key(300.0, serves="VOC-28", unit="s", ge=1)
    voice_idle_factor: float = key(2.0, serves="VOC-28", ge=1, le=10)
    voice_idle_tick_s: float = key(1.0, serves="VOC-28", unit="s", ge=0.01, le=10)
    # How long a sentence waits for a Live session still opening before the verbatim voice reads it instead.
    voice_live_wait_s: float = key(1.0, serves="VOC-28", unit="s", ge=0, le=10)
    # Sentences one Live session reads before a fresh one replaces it, keeping its context small.
    voice_live_max_sentences: int = key(40, serves="VOC-28", ge=1, le=1000)
    # The device has this long to start answering a forwarded chat-completions request (VOC-40 keeps it talking).
    voice_llm_timeout_s: float = key(60.0, serves="VOC-40", unit="s", ge=5)
    # Ask anything (STU-20, D117): the model that writes the cited answer and the search backend it reads from.
    ask_model: str = Field(default="primary/fast-model", pattern=r"^[a-z][a-z0-9_-]*/[a-z][a-z0-9_-]*$")
    ask_search: AskSearch | None = None
    podcast_speech: PodcastSpeech = Field(default_factory=PodcastSpeech)
    sam: Sam = Field(default_factory=Sam)
    segmentation: Segmentation = Field(default_factory=Segmentation)
    research: Research = Field(default_factory=Research)
    companion: CompanionConfig = Field(default_factory=CompanionConfig)
    ask_search_results: int = key(6, serves="STU-20", unit="sources", ge=1, le=10)
    ask_max_tokens: int = key(4096, serves="STU-20", unit="tokens", ge=512, le=16384)
    objectives: Objectives = Field(default_factory=Objectives)
    operations: OperationsPolicy | None = None

    @model_validator(mode="after")
    def _consistent(self) -> GatewayConfig:
        for slot, provider in self.slots.items():
            if provider not in self.providers:
                raise ValueError(f"slot {slot!r} names unknown provider {provider!r}")
            for model in self.providers[provider].models.values():
                if model.id not in self.prices:
                    raise ValueError(f"model {model.id!r} has no price: cost must be computable (MDL-29)")
        primary = self.providers.get(self.slots.get("primary", ""))
        for alias in self.tier.calls_per_turn:
            if primary is not None and alias not in primary.models:
                raise ValueError(f"the tier's usage mix names {alias!r}, not a model of the primary provider (BIZ-08)")
        if self.open_registration and self.environment != "dev":
            raise ValueError("open registration is for the dev environment only")
        if (
            self.quotas.deployment_daily_spend_usd is not None
            or self.quotas.deployment_total_spend_usd is not None
            or self.quotas.deployment_budget_expires_at is not None
        ):
            for amount in (self.quotas.deployment_daily_spend_usd, self.quotas.deployment_total_spend_usd):
                if amount is not None and not math.isfinite(amount):
                    raise ValueError("deployment spend limits must be finite")
            # Cloud voice reserves its speech charges against the budget (`cloud_gateway.voice`); search does not.
            if self.ask_search is not None:
                raise ValueError("the inference budget does not cover search service charges; disable search")
        if (
            self.voice_enabled
            and self.speech.recognizer == "meta"
            and self.voice_first_words_ms > self.voice_meta.ahead_ms
        ):
            raise ValueError("held first words must fit within what speech to text takes ahead of real time")
        google = self.voice_google
        if self.speech.recognizer == "google":
            if not google.project or not google.stt_model or google.stt_minute_usd is None:
                raise ValueError("selected Google recognition requires a project, model and explicit price")
            if self.voice_stt_rotate_s > 290:
                raise ValueError("Google recognition must rotate within its streaming limit")
        if self.speech.synthesizer == "google":
            if not google.project or not google.tts_voices or google.tts_million_chars_usd is None:
                raise ValueError("selected Google synthesis requires a project, voices and explicit price")
        if self.speech.live == "google":
            if (
                not google.live_model
                or not google.live_voice
                or google.live_model not in self.prices
                or (google.live_api == "vertex" and not google.project)
            ):
                raise ValueError("selected Google Live requires an explicit model, voice, price and runtime identity")
        return self


def _merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for name, value in over.items():
        if isinstance(value, dict) and isinstance(merged.get(name), dict) and name not in ("slots", "providers"):
            merged[name] = _merge(merged[name], value)
        else:
            merged[name] = value
    return merged


def load_config(path: Path | None = None, **overrides: Any) -> GatewayConfig:
    """The packaged defaults, the deployment file over them, then keyword overrides. `slots` and `providers` are
    replaced as a whole by a later layer, so a deployment never inherits a provider it did not name."""
    data = _merge({"plans": tomllib.loads(PLANS.read_text())}, tomllib.loads(DEFAULTS.read_text()))
    if path is not None:
        data = _merge(data, tomllib.loads(path.read_text()))
    return GatewayConfig.model_validate(_merge(data, overrides))


def load_deployment(path: Path) -> GatewayConfig:
    """Read the config used to serve Jana requests. Retired demo services cannot be selected."""
    config = load_config(path)
    for provider in config.providers.values():
        host = (urlsplit(provider.base_url).hostname or "").lower()
        if (
            provider.adapter == "meta"
            or host == "meta.ai"
            or host.endswith(".meta.ai")
            or any(model.id.lower().startswith(("muse-spark", "muse-glimmer")) for model in provider.models.values())
        ):
            raise ValueError("Jana deployments cannot select a Meta model provider")
    if (
        config.sam.enabled
        or config.speech.recognizer == "meta"
        or (config.research.enabled and config.research.adapter == "meta_responses")
    ):
        raise ValueError("Jana deployments cannot select Meta speech, research or segmentation")
    return config
