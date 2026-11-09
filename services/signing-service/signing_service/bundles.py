"""Signing the agent bundle of a release (SEC-14, SKL-19): soul, identity, role manifests, guidance, overlays,
templates and the reviewed skills, in one folder whose manifest is signed by an agent-bundle key (TABLE SEC-12a).
Skills that are not `reviewed` are left out and named in the manifest."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agent_core.assets.bundle import ARTEFACT, BUNDLE_FILE, write_bundle
from agent_core.config.trust import Purpose

from signing_service.hierarchy import Signer, SigningRefused, dev_signer, now_iso


def sign_agent_bundle(agent_root: Path, skills_root: Path, out_dir: Path, version: str,
                      signer: Signer | None = None) -> dict[str, Any]:  # fmt: skip
    signer = signer or dev_signer(Purpose.AGENT_BUNDLE)
    if signer.purpose is not Purpose.AGENT_BUNDLE:
        raise SigningRefused(f"{signer.key_id} is a {signer.purpose.value} key; the agent bundle needs its own key")
    created = now_iso()
    manifest = write_bundle(agent_root, skills_root, out_dir, version, created)
    signed = signer.sign(ARTEFACT, manifest, now=created)
    (out_dir / BUNDLE_FILE).write_text(json.dumps(signed, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    return signed
