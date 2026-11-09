"""Verified model files (SEC-24, the M11 subset of model packs; PLT-10 completes them in M20).

A model manifest lists each model the app may run: its runtime id, its GGUF file and optional vision projector, each
with size and sha256, and its weights licence. The manifest is signed like a content pack (Ed25519 over the canonical
manifest without the signature) by a key whose signing root this build trusts. A manifest whose licence is not on
LIST SEC-24a is refused when it is made and when it is read.

The runtime never sees the folder models were installed to. At start the core checks each listed file against the
verified manifest and builds the runtime's models folder from links to the files that match, one folder per model
id, as llama-server's router mode reads them. A file the manifest does not list, or one whose bytes changed, is not
linked, so the runtime cannot load it; why is recorded for the health panel. Hashes are cached by path, size and
modification time, so an unchanged multi-gigabyte file is hashed once.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from agent_core.config.environments import Environment
from agent_core.config.trust import SignatureRefused, TrustStore, bundled_trust
from agent_core.store.packs import signing_bytes

# LIST SEC-24a, items 1 and 2: every other licence needs a recorded decision before a model may ship.
APPROVED_LICENCES = frozenset({"Apache-2.0", "MIT", "BSD-2-Clause", "BSD-3-Clause", "CC-BY-4.0"})
ATTRIBUTION_LICENCES = frozenset({"CC-BY-4.0"})  # shown in the model pack and in Settings → About → Licences
CHUNK = 8 * 1024 * 1024
ARTEFACT = "model_manifest"  # signed by a model intermediate (TABLE SEC-12a)


class ModelManifestError(ValueError):
    """The manifest is unsigned, signed by an untrusted key, changed, or lists a licence not on LIST SEC-24a."""


def licence_problems(manifest: Mapping[str, Any]) -> list[str]:
    problems = []
    for entry in manifest.get("models", []):
        licence = entry.get("licence")
        if licence not in APPROVED_LICENCES:
            problems.append(f"{entry.get('model')}: licence {licence!r} is not on LIST SEC-24a")
        elif licence in ATTRIBUTION_LICENCES and not entry.get("attribution"):
            problems.append(f"{entry.get('model')}: {licence} needs an attribution line")
    return problems


def sign_manifest(manifest: Mapping[str, Any], key: Ed25519PrivateKey, key_id: str) -> dict[str, Any]:
    """Make a signed manifest; refused when a licence is not approved (the model-pack build, SEC-24)."""
    problems = licence_problems(manifest)
    if problems:
        raise ModelManifestError("; ".join(problems))
    unsigned = {k: v for k, v in manifest.items() if k != "signature"}
    value = base64.b64encode(key.sign(signing_bytes(unsigned))).decode()
    return {**unsigned, "signature": {"alg": "ed25519", "key_id": key_id, "value": value}}


def verify_manifest(manifest: Mapping[str, Any], environment: Environment,
                    trust: TrustStore | None = None) -> dict[str, Any]:  # fmt: skip
    """The signature, by a model key chained to a root this build trusts (SEC-12: a content key is refused)."""
    created = manifest.get("created_at")
    try:
        (trust or bundled_trust()).verify(ARTEFACT, manifest, environment,
                                          signed_at=created if isinstance(created, str) else None)  # fmt: skip
    except SignatureRefused as error:
        raise ModelManifestError(f"the model manifest is refused: {error}") from error
    problems = licence_problems(manifest)
    if problems:
        raise ModelManifestError("; ".join(problems))
    return dict(manifest)


@dataclass
class Prepared:
    """The runtime's models folder and what was left out of it, with why."""

    folder: Path
    models: list[str] = field(default_factory=list)
    refused: list[tuple[str, str]] = field(default_factory=list)  # (file or model, reason)


def sha256_of(path: Path, cache: dict[str, Any]) -> str:
    stat = path.stat()
    stamp = [stat.st_size, stat.st_mtime_ns]
    cached = cache.get(str(path))
    if cached and cached["stamp"] == stamp:
        return str(cached["sha256"])
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(CHUNK):
            digest.update(chunk)
    cache[str(path)] = {"stamp": stamp, "sha256": digest.hexdigest()}
    return digest.hexdigest()


def prepare_runtime_models(manifest: Mapping[str, Any], installed: Path, runtime_dir: Path, cache_file: Path,
                           environment: Environment, trust: TrustStore | None = None
                           ) -> Prepared:  # fmt: skip
    """Rebuild `runtime_dir` with links to the installed files the verified manifest lists and that match it."""
    verified = verify_manifest(manifest, environment, trust)
    if runtime_dir.exists():
        shutil.rmtree(runtime_dir)
    runtime_dir.mkdir(parents=True)
    try:
        cache: dict[str, Any] = json.loads(cache_file.read_text()) if cache_file.exists() else {}
    except ValueError:
        cache = {}
    prepared = Prepared(runtime_dir)
    listed: set[str] = set()
    for entry in verified["models"]:
        files = [entry["file"], *([entry["projector"]] if entry.get("projector") else [])]
        listed.update(f["name"] for f in files)
        problem = None
        for spec in files:
            path = installed / spec["name"]
            if not path.is_file():
                problem = (spec["name"], "missing")
            elif path.stat().st_size != int(spec["bytes"]) or sha256_of(path, cache) != spec["sha256"]:
                problem = (spec["name"], "the file does not match its recorded hash")
            if problem:
                break
        if problem:
            prepared.refused.append(problem)
            continue
        folder = runtime_dir / entry["model"]
        folder.mkdir()
        for spec in files:
            os.symlink((installed / spec["name"]).resolve(), folder / spec["name"])
        prepared.models.append(entry["model"])
    for stray in sorted(installed.glob("*.gguf")):
        if stray.name not in listed:
            prepared.refused.append((stray.name, "not listed in a verified model manifest"))
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(json.dumps(cache))
    return prepared
