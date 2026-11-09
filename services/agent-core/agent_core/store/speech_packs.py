"""Signed, immutable speech packs (VOC-02/03). No network or model download path.

Speech weights have their own manifest: the tutor model's 7B floor and model-pack role restrictions remain intact.
A speech pack ships the native voice sidecar, so it is an executable artefact: only an updater-purpose intermediate may
sign it (`speech_pack`, TABLE SEC-12a, D88). Install and every launch check that signature against the device's live
revocation list (SEC-16), the licences, the selected class, the upstream pin and every installed byte.

Licences (VOC-02, D95): model files carry a LIST SEC-24a licence; the runtime (the `sidecar` role: interpreter,
libraries, our launcher) carries a SEC-23 shipped-artefact licence, or an LGPL licence under the D95 conditions: a
separate library in a folder of its own that holds nothing else, its licence text shipped beside it, and never a
model or the entry point. GPL, AGPL and unknown licences are refused everywhere.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from agent_core.config.environments import Environment
from agent_core.config.revocation import BUNDLED as BUNDLED_REVOCATIONS
from agent_core.config.revocation import HELD, RevocationStore, revoked_ids, verify_list
from agent_core.config.trust import SignatureRefused, TrustStore, bundled_trust
from agent_core.store.model_packs import APPROVED_LICENCES

ARTEFACT = "speech_pack"  # updater purpose (D88)
# The live trust (bundled anchors plus the newest revocation list the device adopted, SEC-16), read at each check.
Trust = TrustStore | Callable[[], TrustStore] | None
UPSTREAM_COMMIT = "619e3fe4f71d1c6f35c15870d588a3638de34722"
MANIFEST = "manifest.json"
MANIFEST_LIMIT = 32 * 1024 * 1024  # bytes: a manifest lists every file of the runtime, each with its licence record
HardwareClass = Literal["low", "mid", "high"]
Role = Literal["vad", "turn", "stt", "tts", "sidecar", "wake"]
ENGINES = {
    "vad": {"silero"},
    "turn": {"smart-turn"},
    "stt": {"faster-whisper", "parakeet-tdt"},
    "tts": {"qwen3", "kokoro", "pocket"},
    "sidecar": {"speech-to-speech"},
    "wake": {"sherpa-kws"},  # retired by D126 (VOC-49): parsed in older manifests, never used
}
EXCLUDED = re.compile(r"espeak|phonemizer|mms[-_]tts|livekit|espeakng_loader", re.I)
# A model component (VAD, turn, speech-to-text, text-to-speech) carries weights and data, never code: Python sources or
# native libraries belong to the signed sidecar runtime alone, so no model folder can bring code a loader would run
# (SEC-09; the Silero VAD is TorchScript weights, loaded without torch.hub's hubconf.py).
# VOC-48, D95: the preset speakers of Qwen3-TTS 0.6B CustomVoice (its model card); Kokoro's are its `voices/*.pt` files.
QWEN3_SPEAKERS = frozenset({"vivian", "serena", "uncle_fu", "dylan", "eric", "ryan", "aiden", "ono_anna", "sohee"})
TUTOR_AVATARS = ("avatar.jana", "avatar.orbit", "avatar.prism", "avatar.kite", "avatar.pebble", "avatar.ray")
MODEL_CODE = re.compile(r"\.(?:py|pyc|pyo|pyw|pyd|so|dylib|dll|exe)$|(?:^|/)hubconf\.py$", re.I)
# SEC-23's approved licences for shipped code (the runtime the sidecar role carries); model files keep LIST SEC-24a.
# D95 adds two the speech runtime needs, for the owner to confirm: CNRI-Python (regex, misaki's tokenizer: one part
# of the Python licence stack SEC-23 approves as PSF) and BSL-1.0 (the SLEEF maths library compiled into torch, which
# runs Kokoro on the CPU). `WITH` exceptions only widen a licence and are read as the licence itself (SEC-23's scan).
RUNTIME_LICENCES = frozenset(
    {
        "MIT",
        "MIT-0",
        "MIT-CMU",
        "BSD-2-Clause",
        "BSD-3-Clause",
        "0BSD",
        "Apache-2.0",
        "ISC",
        "Zlib",
        "PSF-2.0",
        "Python-2.0",
        "CC0-1.0",
        "MPL-2.0",
        "Unicode-3.0",
        "Unicode-DFS-2016",
        "CNRI-Python",
        "BSL-1.0",
    }
)
LGPL = re.compile(r"^LGPL-(?:2\.1|3\.0)-(?:only|or-later)$")  # D95: in a speech pack, as a separate library only
LICENCE_TEXT = re.compile(r"^(?:COPYING(?:\.LESSER|\.LGPL)?|LICEN[CS]E)(?:[._-].*)?$", re.I)
PACK_ID = re.compile(r"^sp\.en\.[0-9]+(?:_[0-9]+){0,3}$")
# The highest speech pack version this device has installed, kept beside the packs: a pack older than it is refused
# even after the newer one was removed (SEC-15's rule for packs: roll forward, never back).
VERSION_FLOOR = ".version-floor.json"
# Reasons a pack does not apply to this device, rather than failing its checks: a newer pack refused for one of these
# leaves older packs usable; one refused for anything else (a revoked signer, damage) leaves none (SEC-16).
NOT_APPLICABLE = frozenset({"speech_pack_platform_refused", "speech_pack_app_too_old"})


class SpeechPackError(ValueError):
    """A reason-only diagnostic, safe for the health panel and logs."""


class _Record(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Licence(_Record):
    spdx: str
    source_url: str = Field(min_length=1, max_length=2000)
    attribution: str = Field(min_length=1, max_length=20000)


class SpeechFile(_Record):
    path: str = Field(min_length=1, max_length=500)
    role: Role
    engine: str
    classes: list[HardwareClass] = Field(min_length=1, max_length=3)
    size_bytes: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    licence: Licence


class SpeechProfile(_Record):
    """Pack-relative component roots. Runtime-specific options are fixed by our launcher, never supplied as code.

    `stt: none` is a read-aloud-only profile (TABLE VOC-03a): no speech-to-text, no VAD or turn model, and no audio
    is ever sent to it; the sidecar then serves the speak directive alone (VOC-18, VOC-39)."""

    stt: Literal["faster-whisper", "parakeet-tdt", "none"]
    tts: Literal["qwen3", "kokoro", "pocket"]
    device: Literal["cpu", "cuda", "mps"]
    vad_path: str | None = None
    turn_path: str | None = None
    stt_path: str | None = None
    # The wake-word detector's model folder (VOC-49), retired by D126: still parsed so an older manifest reads, and
    # ignored; no pack the build produces sets it (VOC-50).
    wake_path: str | None = None
    tts_path: str
    sidecar_path: str
    speaker: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_ -]+$")
    # VOC-48: each tutor character's preset speaker on this class; a character not named uses `speaker`.
    tutor_speakers: dict[str, str] = Field(default_factory=dict, max_length=6)

    @property
    def read_aloud_only(self) -> bool:
        return self.stt == "none"


class SpeechManifest(_Record):
    speech_pack_id: str = Field(pattern=r"^sp\.en\.[0-9]+(?:_[0-9]+){0,3}$")
    manifest_version: Literal[1]
    language: Literal["en"]
    platform: Literal["macos-aarch64", "macos-x86_64", "linux-x86_64", "windows-x86_64"]
    upstream_commit: Literal["619e3fe4f71d1c6f35c15870d588a3638de34722"]
    min_app_version: str = Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$")
    created_at: str
    profiles: dict[HardwareClass, SpeechProfile] = Field(min_length=1, max_length=3)
    files: list[SpeechFile] = Field(min_length=1, max_length=50000)
    signature: dict[str, str]


def _relative(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if path.is_absolute() or any(p in ("", ".", "..") for p in value.split("/")) or ":" in value or "\\" in value:
        raise SpeechPackError("speech_pack_path_refused")
    return path


def _local(root: Path, value: str) -> Path:
    path = root.joinpath(*_relative(value).parts)
    if root.is_symlink() or any(parent.is_symlink() for parent in (path, *path.parents) if parent != root.parent):
        raise SpeechPackError("speech_pack_symlink_refused")
    return path


def _alternatives(spdx: str) -> list[list[str]]:
    """An SPDX expression as its OR alternatives, each a list of AND terms (a `WITH` exception read as its licence)."""
    expression = re.sub(r"\s+WITH\s+[\w.+-]+", "", spdx).replace("(", " ").replace(")", " ")
    return [
        [term for term in re.split(r"\s+AND\s+", part.strip()) if term] for part in re.split(r"\s+OR\s+", expression)
    ]


def licence_use(spdx: str, role: str) -> Literal["approved", "lgpl", "refused"]:
    """VOC-02 and D95: whether a component's licence may ship in its role. Models: LIST SEC-24a. The runtime: SEC-23,
    preferring an alternative without LGPL; an LGPL alternative is allowed only as a separate library (`lgpl`)."""
    if role != "sidecar":
        return "approved" if spdx in APPROVED_LICENCES else "refused"
    options = _alternatives(spdx)
    if any(terms and all(term in RUNTIME_LICENCES for term in terms) for terms in options):
        return "approved"
    if any(terms and all(term in RUNTIME_LICENCES or LGPL.match(term) for term in terms) for terms in options):
        return "lgpl"
    return "refused"


def _lgpl_separate(files: list[SpeechFile], lgpl: list[SpeechFile], entrypoints: set[str]) -> None:
    """D95: each LGPL library sits in a folder of its own that holds only that library's files (the same licence
    record), is never an entry point, and ships its licence text, so it stays a separate, replaceable library."""
    folders: dict[str, tuple[str, str]] = {}
    for file in lgpl:
        folder = PurePosixPath(file.path).parent.as_posix()
        record = (file.licence.spdx, file.licence.source_url)
        if file.path in entrypoints or folder == "." or folders.setdefault(folder, record) != record:
            raise SpeechPackError("speech_pack_licence_refused")
    for folder, record in folders.items():
        inside = [f for f in files if f.path.startswith(folder + "/")]
        if any((f.licence.spdx, f.licence.source_url) != record for f in inside):
            raise SpeechPackError("speech_pack_licence_refused")
        if not any(
            (f.licence.spdx, f.licence.source_url) == record and LICENCE_TEXT.match(PurePosixPath(f.path).name)
            for f in files
        ):
            raise SpeechPackError("speech_pack_licence_refused")


def _version(text: str) -> tuple[int, ...]:
    return tuple(int(v) for v in text.split("-", 1)[0].split("."))


def pack_version(pack_id: str) -> tuple[int, ...]:
    """A pack id's version (`sp.en.1_2` is 1.2), trailing zeros dropped so `sp.en.1` and `sp.en.1_0` compare equal."""
    if not PACK_ID.match(pack_id):
        raise SpeechPackError("speech_pack_id_refused")
    parts = [int(v) for v in pack_id.split(".")[-1].split("_")]
    while len(parts) > 1 and parts[-1] == 0:
        parts.pop()
    return tuple(parts)


def shipped_trust(environment: Environment) -> TrustStore:
    """Without the device's live list (a process with no data folder), at least the list this build shipped with."""
    try:
        document = json.loads(BUNDLED_REVOCATIONS.read_text(encoding="utf-8"))
        verify_list(document, environment)
        return bundled_trust().with_revoked(revoked_ids(document))
    except (OSError, ValueError, KeyError, TypeError, SignatureRefused):
        return bundled_trust()


def held_trust(data_root: Path, environment: Environment) -> TrustStore:
    """The shipped anchors and list plus the device's held revocation list in `data_root`, read without writing.

    For the sidecar process: it must not start a pack whose signer was revoked by a list the device adopted after the
    core checked it (SEC-16, D88). A held list that is missing, edited or unverifiable adds nothing.
    """
    base = shipped_trust(environment)
    try:
        document = json.loads((data_root / HELD).read_text(encoding="utf-8"))
        verify_list(document, environment)
    except (OSError, ValueError, KeyError, TypeError, SignatureRefused):
        return base
    return base.with_revoked(revoked_ids(document))


def resolve_trust(trust: Trust, environment: Environment) -> TrustStore:
    if trust is None:
        return shipped_trust(environment)
    return trust if isinstance(trust, TrustStore) else trust()


def read_manifest(folder: Path, environment: Environment, *, trust: Trust = None) -> SpeechManifest:
    try:
        path = _local(folder, MANIFEST)
        if path.stat().st_size > MANIFEST_LIMIT:
            raise SpeechPackError("speech_pack_manifest_limit")
        raw = path.read_bytes()
        document = json.loads(raw)
        manifest = SpeechManifest.model_validate(document)
        resolve_trust(trust, environment).verify(ARTEFACT, document, environment, manifest.created_at)
    except (OSError, ValueError, TypeError, ValidationError, SignatureRefused) as error:
        raise SpeechPackError("speech_pack_manifest_refused") from error
    check_components(manifest)
    return manifest


def check_components(manifest: SpeechManifest) -> None:
    """The licence gate and the class profiles (VOC-02, VOC-03, D95), re-run at every read and by the pack build and
    the signing command before a signature exists."""
    names = set()
    lgpl: list[SpeechFile] = []
    for file in manifest.files:
        _relative(file.path)
        name = file.path.casefold()
        if name in names or name == MANIFEST or EXCLUDED.search(name):
            raise SpeechPackError("speech_pack_component_refused")
        names.add(name)
        use = licence_use(file.licence.spdx, file.role)
        if file.engine not in ENGINES[file.role] or use == "refused":
            raise SpeechPackError("speech_pack_licence_refused")
        if file.role != "sidecar" and MODEL_CODE.search(name):
            raise SpeechPackError("speech_pack_component_refused")
        if use == "lgpl":
            lgpl.append(file)
        if not file.licence.source_url.startswith("https://"):
            raise SpeechPackError("speech_pack_licence_refused")
    _lgpl_separate(manifest.files, lgpl, {profile.sidecar_path for profile in manifest.profiles.values()})
    for hardware, profile in manifest.profiles.items():
        roots = {
            "vad": profile.vad_path,
            "turn": profile.turn_path,
            "stt": profile.stt_path,
            "tts": profile.tts_path,
            "sidecar": profile.sidecar_path,
            "wake": profile.wake_path,
        }
        if (
            profile.read_aloud_only != (profile.stt_path is None)
            or (not profile.read_aloud_only and profile.vad_path is None)
            or (profile.read_aloud_only and profile.wake_path is not None)
        ):
            raise SpeechPackError("speech_pack_profile_refused")
        if hardware != "low" and not profile.read_aloud_only and profile.turn_path is None:
            raise SpeechPackError("speech_pack_turn_model_missing")
        for role, directory in roots.items():
            if directory is None:
                continue
            _relative(directory)
            matches = [
                f
                for f in manifest.files
                if f.role == role
                and hardware in f.classes
                and (f.path == directory or f.path.startswith(directory.rstrip("/") + "/"))
            ]
            if not matches or any(f.engine != getattr(profile, role) for f in matches if role in ("stt", "tts")):
                raise SpeechPackError("speech_pack_profile_refused")


def _preset_speakers(files: list[SpeechFile], hardware: str, profile: SpeechProfile) -> None:
    """VOC-48, D95: every speaker a class names, its default and each character's, is a preset speaker of that class's
    text-to-speech model: a Kokoro voice file the pack ships for the class, or one of Qwen3-TTS CustomVoice's preset
    speakers. Any other engine keeps its default speaker for every character. Cloning options cannot be written at
    all: the profile record refuses every field it does not name."""
    for avatar, speaker in profile.tutor_speakers.items():
        if avatar not in TUTOR_AVATARS or not re.fullmatch(r"[A-Za-z0-9_ -]{1,80}", speaker):
            raise SpeechPackError("speech_pack_speaker_refused")
    for speaker in {profile.speaker, *profile.tutor_speakers.values()}:
        if profile.tts == "kokoro":
            voice = f"{profile.tts_path.rstrip('/')}/voices/{speaker}.pt"
            offered = any(f.path == voice and f.role == "tts" and hardware in f.classes for f in files)
        elif profile.tts == "qwen3":
            offered = speaker.lower() in QWEN3_SPEAKERS
        else:
            offered = speaker == profile.speaker
        if not offered:
            raise SpeechPackError("speech_pack_speaker_refused")


def check_folder(folder: Path) -> SpeechManifest:
    """A pack folder before it is signed: its manifest, the licence gate and the class profiles, and every byte against
    the manifest with nothing unlisted. For the pack build and the signing command (VOC-02, VOC-03, D88); a device
    never trusts a pack this way, only through `read_manifest` and `verify_pack`."""
    try:
        path = _local(folder, MANIFEST)
        if path.stat().st_size > MANIFEST_LIMIT:
            raise SpeechPackError("speech_pack_manifest_limit")
        manifest = SpeechManifest.model_validate(json.loads(path.read_bytes()))
    except (OSError, ValueError, TypeError, ValidationError) as error:
        raise SpeechPackError("speech_pack_manifest_refused") from error
    check_components(manifest)
    for hardware, profile in manifest.profiles.items():
        _preset_speakers(manifest.files, hardware, profile)  # the build and signing gate (VOC-48)
    try:
        present = _inventory(folder)
        for file in manifest.files:
            if present.get(file.path) != file.size_bytes:
                raise SpeechPackError("speech_pack_file_refused")
            with _local(folder, file.path).open("rb") as source:
                if hashlib.file_digest(source, "sha256").hexdigest() != file.sha256:
                    raise SpeechPackError("speech_pack_file_refused")
    except OSError as error:
        raise SpeechPackError("speech_pack_file_refused") from error
    if set(present) - {file.path for file in manifest.files} - {MANIFEST}:
        raise SpeechPackError("speech_pack_unlisted_file")
    return manifest


def _inventory(folder: Path) -> dict[str, int]:
    """Every regular file under `folder` (pack-relative POSIX path -> size), in one walk that refuses any symlink or
    special file: what a launch checks against the manifest without stat-ing every path's parents again."""
    if folder.is_symlink():
        raise SpeechPackError("speech_pack_symlink_refused")
    sizes: dict[str, int] = {}
    pending = [(folder, "")]
    while pending:
        directory, prefix = pending.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                relative = prefix + entry.name
                if entry.is_symlink():
                    raise SpeechPackError("speech_pack_symlink_refused")
                if entry.is_dir(follow_symlinks=False):
                    pending.append((Path(entry.path), relative + "/"))
                elif entry.is_file(follow_symlinks=False):
                    sizes[relative] = entry.stat(follow_symlinks=False).st_size
                else:
                    raise SpeechPackError("speech_pack_file_refused")
    return sizes


def verify_pack(
    folder: Path,
    hardware: HardwareClass,
    platform: str,
    app_version: str,
    environment: Environment,
    *,
    trust: Trust = None,
    hashes: bool = True,
) -> SpeechManifest:
    """The signed manifest and the installed bytes. `hashes=False` skips only the content digests: for the sidecar,
    which the core starts only after it verified every byte with the live trust for this launch (VOC-23)."""
    manifest = read_manifest(folder, environment, trust=trust)
    if hardware not in manifest.profiles or manifest.platform != platform:
        raise SpeechPackError("speech_pack_platform_refused")
    if _version(app_version) < _version(manifest.min_app_version):
        raise SpeechPackError("speech_pack_app_too_old")
    try:
        present = _inventory(folder)
        for file in manifest.files:
            size = present.get(file.path)
            if size is None and hardware not in file.classes:
                continue  # another class's component: the installer left it out (VOC-03)
            if size != file.size_bytes:
                raise SpeechPackError("speech_pack_file_refused")
            if not hashes:
                continue
            with _local(folder, file.path).open("rb") as source:
                if hashlib.file_digest(source, "sha256").hexdigest() != file.sha256:
                    raise SpeechPackError("speech_pack_file_refused")
    except OSError as error:
        raise SpeechPackError("speech_pack_file_refused") from error
    if set(present) - {file.path for file in manifest.files} - {MANIFEST}:
        raise SpeechPackError("speech_pack_unlisted_file")
    return manifest


class SpeechPackStore:
    def __init__(
        self,
        root: Path,
        environment: Environment,
        platform: str,
        app_version: str,
        hardware: HardwareClass,
        *,
        trust: Trust = None,
    ) -> None:
        self.root, self.environment, self.platform = root, environment, platform
        self.app_version, self.hardware, self.trust = app_version, hardware, trust
        # The device's held list lives in the data folder that holds `speech-packs/` (DAT-01a). Read at every check
        # as well as the caller's live trust, so a list adopted through any unrelated update applies here at once,
        # even for a store built with a fixed trust (SEC-16, D88).
        self.revocations = RevocationStore(root.parent, environment, trust if isinstance(trust, TrustStore) else None)

    def current_trust(self) -> TrustStore:
        """The caller's trust (live or fixed) with every key the device's held revocation list withdraws refused."""
        return resolve_trust(self.trust, self.environment).with_revoked(self.revocations.revoked())

    def verify(self, folder: Path) -> SpeechManifest:
        return verify_pack(
            folder, self.hardware, self.platform, self.app_version, self.environment, trust=self.current_trust()
        )

    def version_floor(self) -> tuple[int, ...]:
        """The oldest version this device may still install: the newest it has installed, whether that pack is still
        here or was removed since (recorded in `VERSION_FLOOR`), or nothing on a fresh device."""
        versions = [pack_version(f.name) for f in self.root.glob("sp.en.*") if PACK_ID.match(f.name)]
        try:
            recorded = json.loads((self.root / VERSION_FLOOR).read_text(encoding="utf-8"))["speech_pack_id"]
            versions.append(pack_version(recorded))
        except (OSError, ValueError, KeyError, TypeError):
            pass
        return max(versions, default=())

    def _raise_floor(self, pack_id: str) -> None:
        floor = self.root / VERSION_FLOOR
        try:
            recorded = pack_version(json.loads(floor.read_text(encoding="utf-8"))["speech_pack_id"])
        except (OSError, ValueError, KeyError, TypeError):
            recorded = ()
        if pack_version(pack_id) <= recorded:
            return
        pending = self.root / f"{VERSION_FLOOR}.partial"
        pending.write_text(json.dumps({"speech_pack_id": pack_id}), encoding="utf-8")
        os.replace(pending, floor)

    def install(self, source: Path) -> Path:
        manifest = self.verify(source)
        if self.root.is_symlink():
            raise SpeechPackError("speech_pack_symlink_refused")
        self.root.mkdir(parents=True, exist_ok=True)
        destination = self.root / manifest.speech_pack_id
        if destination.exists():
            if self.verify(destination).model_dump() != manifest.model_dump():
                raise SpeechPackError("speech_pack_version_collision")
            return destination
        if pack_version(manifest.speech_pack_id) < self.version_floor():
            raise SpeechPackError("speech_pack_older_than_installed")  # SEC-15: packs roll forward only
        staging = Path(tempfile.mkdtemp(prefix=".speech-staging-", dir=self.root))
        try:
            shutil.copyfile(source / MANIFEST, staging / MANIFEST)
            for file in manifest.files:
                if self.hardware in file.classes:
                    target = staging / file.path
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source / file.path, target)
            self.verify(staging)
            os.rename(staging, destination)
        finally:
            if staging.exists():
                shutil.rmtree(staging)
        self._raise_floor(manifest.speech_pack_id)
        return destination

    def licences(self, folder: Path) -> list[dict[str, Any]]:
        return self._licences(self.verify(folder))

    def _licences(self, manifest: SpeechManifest) -> list[dict[str, Any]]:
        return [
            {"path": f.path, "role": f.role, "engine": f.engine, "sha256": f.sha256, **f.licence.model_dump()}
            for f in manifest.files
            if self.hardware in f.classes
        ]

    def installed(self) -> list[dict[str, Any]]:
        """Every installed speech pack with the licence and attribution of each component this class runs, for
        Settings → About → Licences and the pack list (VOC-02, PLT-10). The signature is checked with the live trust
        here; every byte is checked at install and at each sidecar launch (VOC-03), so listing never hashes weights."""
        rows: list[dict[str, Any]] = []
        for folder in sorted(self.root.glob("sp.en.*")) if self.root.is_dir() else []:
            if not PACK_ID.match(folder.name):
                continue
            try:
                manifest = read_manifest(folder, self.environment, trust=self.current_trust())
            except SpeechPackError:
                rows.append({"speech_pack_id": folder.name, "kind": ARTEFACT, "signature_ok": False, "licences": []})
                continue
            licences = self._licences(manifest)
            rows.append(
                {
                    "speech_pack_id": manifest.speech_pack_id,
                    "kind": ARTEFACT,
                    "language": manifest.language,
                    "signature_ok": manifest.speech_pack_id == folder.name,
                    "size_bytes": sum(f.size_bytes for f in manifest.files if self.hardware in f.classes),
                    "licences": licences,
                    "attributions": sorted({row["attribution"] for row in licences}),
                }
            )
        return rows

    def remove(self, pack_id: str) -> bool:
        """Delete an installed speech pack (Settings, the pack list). The folder is first renamed out of the way, so a
        half-deleted pack is never offered; the caller stops a sidecar running from it before this (VOC-03)."""
        if not PACK_ID.match(pack_id) or self.root.is_symlink():
            return False
        folder = self.root / pack_id
        if folder.is_symlink():
            raise SpeechPackError("speech_pack_symlink_refused")
        if not folder.is_dir():
            return False
        doomed = Path(tempfile.mkdtemp(prefix=".speech-removing-", dir=self.root))
        try:
            os.rename(folder, doomed / pack_id)
        except OSError as error:
            shutil.rmtree(doomed, ignore_errors=True)
            raise SpeechPackError("speech_pack_in_use") from error
        shutil.rmtree(doomed, ignore_errors=True)
        return True
