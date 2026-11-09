"""Signing a speech pack with the Updater key (D88, SEC-12a, VOC-03).

A speech pack ships the native voice sidecar, so only an updater-purpose intermediate signs it. Before any signature,
the pack is checked exactly as the app will check it (the licence gate with D95's LGPL rule, the class profiles) and
every byte is hashed against its manifest; afterwards the signed manifest is read back with the trust that will
verify it on devices. The signing time becomes the manifest's `created_at`, which devices check the key against.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agent_core.config import Environment
from agent_core.config.trust import TrustStore
from agent_core.store.speech_packs import ARTEFACT, MANIFEST, SpeechPackError, check_folder, read_manifest

from signing_service.hierarchy import Signer, SigningRefused, now_iso


def sign_speech_pack(
    folder: Path, signer: Signer, trust: TrustStore, environment: Environment, *, now: str | None = None
) -> dict[str, Any]:
    """Sign `folder/manifest.json` in place after the full pre-signing check; returns the signed manifest."""
    try:
        check_folder(folder)
    except SpeechPackError as error:
        raise SigningRefused(f"the speech pack fails its own gate ({error}); nothing was signed") from error
    document = json.loads((folder / MANIFEST).read_text())
    moment = now or now_iso()
    document["created_at"] = moment
    signed = signer.sign(ARTEFACT, document, now=moment)
    path = folder / MANIFEST
    previous = path.read_bytes()
    path.write_text(json.dumps(signed, indent=1, sort_keys=True) + "\n")
    try:
        read_manifest(folder, environment, trust=trust)
    except SpeechPackError as error:
        path.write_bytes(previous)
        raise SigningRefused(f"the signed manifest does not verify for {environment.value} ({error})") from error
    return signed
