"""The signed agent bundle (SEC-14, SKL-19, PRM-05).

The soul and identity, role manifests, guidance blocks, family overlays, templates, the capture filter, notices, the
course glossary and the reviewed skills ship as one bundle per release: a folder with `agent/`, `skills/` and
`bundle.json`, the manifest that lists every file's path, size and sha256 and every skill's name, version, body
sha256 and status. The manifest is signed by an agent-bundle key (TABLE SEC-12a). Only skills with status `reviewed`
are bundled, and a bundle that lists any other is refused.

At start the app takes the newest installed bundle that verifies: signature, every listed file's hash, no file the
manifest does not list, and every skill reviewed. A bundle edited on disk (a changed role manifest, say) fails, and
the previous verified bundle is used instead; why is recorded for the health panel. Installed bundle files are
read-only: the running system never writes to the bundle (agent proposals for skill changes go to the authoring queue
off the device, SKL-20).
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agent_core.assets import rules
from agent_core.assets.skills import KINDS, SkillError, discover, parse
from agent_core.config.environments import Environment
from agent_core.config.trust import SignatureRefused, TrustStore, bundled_trust

FORMAT = "jana.agent-bundle.1"
ARTEFACT = "agent_bundle"
BUNDLE_FILE = "bundle.json"
AGENT = "agent"
SKILLS = "skills"
# The agent content the device runs from; authoring-only prompts (`drafting/`) stay off the device.
AGENT_DIRS = ("soul", "roles", "guidance", "families", "templates", "memory", "notices", "rules", "safety", "glossary")
REVIEWED = "reviewed"
READ_ONLY = 0o444


class BundleRefused(ValueError):
    """No installed bundle verifies, or this one does not."""


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _files_under(folder: Path) -> list[Path]:
    return sorted(p for p in folder.rglob("*") if p.is_file()) if folder.is_dir() else []


def bundle_sources(agent_root: Path, skills_root: Path) -> tuple[dict[str, Path], list[dict[str, Any]], list[str]]:
    """What a release bundles: published path -> source file, the bundled skills' manifest rows, and the skills left
    out because they are not reviewed."""
    problems = rules.folder_errors(agent_root / "rules")  # a rule without a passing scenario blocks the build (PED-17)
    if problems:
        raise BundleRefused("; ".join(problems[:5]))
    files: dict[str, Path] = {}
    for directory in AGENT_DIRS:
        for path in _files_under(agent_root / directory):
            files[f"{AGENT}/{path.relative_to(agent_root).as_posix()}"] = path
    skills, left_out = [], []
    for skill_file in discover(skills_root, KINDS):
        skill = parse(skill_file)
        if skill.status != REVIEWED:
            left_out.append(skill.name)
            continue
        for path in _files_under(skill.folder):
            if "evals" in path.relative_to(skill.folder).parts:
                continue  # colocated evals run in the evaluation harness, never on a device (SKL-21)
            files[f"{SKILLS}/{path.relative_to(skills_root).as_posix()}"] = path
        skills.append({"name": skill.name, "kind": skill.kind, "version": skill.version, "sha256": skill.sha256,
                       "status": skill.status})  # fmt: skip
    return files, sorted(skills, key=lambda s: str(s["name"])), sorted(left_out)


def write_bundle(agent_root: Path, skills_root: Path, out_dir: Path, version: str, created_at: str) -> dict[str, Any]:
    """Copy a release's bundle files into `out_dir`; returns the unsigned manifest (the signing service signs it)."""
    files, skills, left_out = bundle_sources(agent_root, skills_root)
    out_dir.mkdir(parents=True, exist_ok=True)
    listed = []
    for published, source in sorted(files.items()):
        target = out_dir / published
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        listed.append({"path": published, "bytes": target.stat().st_size, "sha256": _sha256(target)})
    return {"format": FORMAT, "bundle_id": "agent", "version": version, "created_at": created_at, "files": listed,
            "skills": skills, "left_out_unreviewed": left_out}  # fmt: skip


def verify_bundle(folder: Path, environment: Environment, trust: TrustStore | None = None) -> dict[str, Any]:
    """The bundle's manifest after checking its signature, every file and every skill; raises `BundleRefused`."""
    try:
        manifest = json.loads((folder / BUNDLE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise BundleRefused(f"{folder.name}: no readable {BUNDLE_FILE}") from error
    if not isinstance(manifest, dict) or manifest.get("format") != FORMAT:
        raise BundleRefused(f"{folder.name}: not an agent bundle")
    created = manifest.get("created_at")
    try:
        (trust or bundled_trust()).verify(ARTEFACT, manifest, environment,
                                          signed_at=created if isinstance(created, str) else None)  # fmt: skip
    except SignatureRefused as error:
        raise BundleRefused(f"{folder.name}: {error}") from error
    listed = {str(f["path"]): f for f in manifest.get("files", [])}
    present = {p.relative_to(folder).as_posix() for p in _files_under(folder)} - {BUNDLE_FILE}
    for extra in sorted(present - set(listed)):
        raise BundleRefused(f"{folder.name}: {extra} is not in the signed manifest")
    for path, entry in sorted(listed.items()):
        file = folder / path
        if not file.is_file():
            raise BundleRefused(f"{folder.name}: {path} is missing")
        if file.stat().st_size != int(entry["bytes"]) or _sha256(file) != entry["sha256"]:
            raise BundleRefused(f"{folder.name}: {path} differs from the signed manifest")
    for row in manifest.get("skills", []):
        if row.get("status") != REVIEWED:
            raise BundleRefused(f"{folder.name}: skill {row.get('name')} is {row.get('status')}, not reviewed")
    bundled = {str(s["name"]): s for s in manifest.get("skills", [])}
    for skill_file in discover(folder / SKILLS, KINDS):
        try:
            skill = parse(skill_file)
        except SkillError as error:
            raise BundleRefused(f"{folder.name}: {skill_file.relative_to(folder)}: {error}") from error
        row = bundled.get(skill.name)
        if row is None or skill.status != REVIEWED or skill.sha256 != row["sha256"] or skill.version != row["version"]:
            raise BundleRefused(f"{folder.name}: skill {skill.name} does not match the signed manifest")
    return dict(manifest)


def _version_key(name: str) -> tuple[int, ...]:
    try:
        return tuple(int(p) for p in name.split("."))
    except ValueError:
        return (-1,)


@dataclass(frozen=True)
class SelectedBundle:
    folder: Path
    version: str
    refused: list[tuple[str, str]] = field(default_factory=list)  # newer bundles that did not verify, and why

    @property
    def agent_root(self) -> Path:
        return self.folder / AGENT

    @property
    def skills_root(self) -> Path:
        return self.folder / SKILLS


def select_bundle(bundles_dir: Path, environment: Environment, trust: TrustStore | None = None,
                  at_most: str | None = None) -> SelectedBundle:  # fmt: skip
    """The newest installed bundle (up to `at_most`, the version the shell names) that verifies; newer ones that do
    not are listed in `refused`."""
    refused: list[tuple[str, str]] = []
    cap = _version_key(at_most) if at_most else None
    candidates = [p for p in bundles_dir.iterdir() if p.is_dir() and _version_key(p.name)[0] >= 0
                  and (cap is None or _version_key(p.name) <= cap)] if bundles_dir.is_dir() else []  # fmt: skip
    for folder in sorted(candidates, key=lambda p: _version_key(p.name), reverse=True):
        try:
            manifest = verify_bundle(folder, environment, trust)
        except BundleRefused as error:
            refused.append((folder.name, str(error)))
            continue
        if manifest.get("version") != folder.name:
            refused.append((folder.name, f"{folder.name}: the manifest is version {manifest.get('version')}"))
            continue
        return SelectedBundle(folder, folder.name, refused)
    raise BundleRefused("no installed agent bundle verifies: " + "; ".join(r for _, r in refused) if refused
                        else f"no agent bundle is installed in {bundles_dir}")  # fmt: skip


def rollback_bundle(bundles_dir: Path, environment: Environment, current: str,
                    trust: TrustStore | None = None) -> SelectedBundle:  # fmt: skip
    """SKL-19 rollback: the newest installed bundle older than `current` that verifies. It reads only what is
    installed on this device, so it works with networking off; the shell then names that version at start."""
    older = [p.name for p in bundles_dir.iterdir() if p.is_dir() and 0 <= _version_key(p.name)[0]
             and _version_key(p.name) < _version_key(current)] if bundles_dir.is_dir() else []  # fmt: skip
    if not older:
        raise BundleRefused(f"no agent bundle older than {current} is installed to roll back to")
    return select_bundle(bundles_dir, environment, trust, at_most=max(older, key=_version_key))


def install_bundle(source: Path, bundles_dir: Path, environment: Environment, trust: TrustStore | None = None) -> Path:
    """Install a signed bundle as `<bundles_dir>/<version>/`, read-only, after it verifies; earlier versions stay so
    the app can fall back to them (SKL-19 offline rollback)."""
    manifest = verify_bundle(source, environment, trust)
    target = bundles_dir / str(manifest["version"])
    if target.exists():
        raise BundleRefused(f"agent bundle {manifest['version']} is already installed")
    staging = bundles_dir / f".{manifest['version']}.partial"
    shutil.rmtree(staging, ignore_errors=True)
    shutil.copytree(source, staging)
    verify_bundle(staging, environment, trust)
    _read_only(_files_under(staging))
    os.replace(staging, target)
    return target


def _read_only(paths: Iterable[Path]) -> None:
    for path in paths:
        os.chmod(path, READ_ONLY)


def listing(manifest: Mapping[str, Any]) -> list[str]:
    """The bundled paths, for diagnostics."""
    return [str(f["path"]) for f in manifest.get("files", [])]
