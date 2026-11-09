"""The key ceremony (SEC-12, TABLE SEC-12a, SEC-16, OPS-35, D88): the steps that make the release root's public record,
the five intermediate certificates and the root's first revocation list, and put them in the trust anchors the app
bundles. The guide is scripts/governance/evidence/KEY-CEREMONY.md.

The private keys never pass through here. The root signs through its HSM's signing command (`hierarchy.CommandSigner`:
bytes on stdin, base64 signature on stdout), on the offline ceremony machine; each intermediate is generated in its own
HSM and only its public key is named. What this writes is public: it is what the app ships.

    python -m signing_service.ceremony issue --out DIR --root-key-id ID --root-public-key B64
        --root-sign-command "CMD ..." --intermediate PURPOSE=KEY_ID=B64 (one per purpose) [--not-before ISO]
        [--root release|staging]
    python -m signing_service.ceremony adopt DIR/ceremony-record.json [--anchors PATH]
    python -m signing_service.ceremony dry-run [--out DIR]
    python -m signing_service.ceremony sign-with-key PEM    (the dry run's stand-in for an HSM signing command)
    python -m signing_service.ceremony sign-updates FOLDER --sign-command "CMD ..." --base-url URL [--pubkey KEY]

`dry-run` rehearses every step with throwaway keys made in a temporary folder: it issues, adopts into a copy of the
bundled anchors (never the repository's), signs a document of every artefact type with the matching intermediate
through the same command path, checks that a release build verifies them and refuses a speech pack from the wrong key
(D88), and prints what the real ceremony will run. Its record is marked `dry_run` and `adopt` refuses it against the
repository's anchors.

`sign-updates` is the per-release step on the same offline laptop (owner ruling 2026-10-17: app updates are signed
offline, and no updater key is held in CI): it signs the update bundles the release job left unsigned, with Tauri's
updater key through its signing command, and writes the signed result publishing takes (`signing_service.updater`).
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import shlex
import shutil
import sys
import tempfile
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from agent_core.config import Settings
from agent_core.config.environments import Environment
from agent_core.config.revocation import verify_list
from agent_core.config.trust import ANCHORS, ARTEFACT_PURPOSE, Purpose, SignatureRefused, TrustStore
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, load_pem_private_key

from signing_service.hierarchy import (
    ROTATION_KEY,
    TEST_ROOT,
    CommandSigner,
    SigningRefused,
    anchors_text,
    issue_certificate,
    production_anchors,
    production_signer,
    public_b64,
    root_record,
    verify_issued,
)
from signing_service.revocation import revocation_list, sign_with_root
from signing_service.updater import rehearse, sign_main

RECORD = "ceremony-record.json"
REVOCATION = "revocation_list.json"
KIND = "key_ceremony"
ROOTS = {"release": Environment.PRODUCTION, "staging": Environment.STAGING}
DRY_RUN_PREFIX = "key_dryrun_"


class CeremonyRefused(ValueError):
    """A step of the ceremony would produce anchors the app must not trust."""


def _iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def _after_years(start: str, years: int) -> str:
    """The end of a validity of `years` from `start`, as long as the rotation allows and no longer (whole days of
    365.25, the year `hierarchy` measures with)."""
    moment = datetime.fromisoformat(start.replace("Z", "+00:00"))
    return _iso(moment + timedelta(days=int(years * 365.25)))


def _intermediates(specs: Sequence[str]) -> dict[Purpose, tuple[str, str]]:
    found: dict[Purpose, tuple[str, str]] = {}
    for spec in specs:
        purpose, key_id, public_key = [*spec.split("=", 2), "", ""][:3]
        if purpose not in {p.value for p in Purpose} or not key_id or not public_key:
            raise CeremonyRefused(f"--intermediate {spec!r}: want PURPOSE=KEY_ID=PUBLIC_KEY_B64")
        if Purpose(purpose) in found:
            raise CeremonyRefused(f"two intermediates for {purpose}")
        found[Purpose(purpose)] = (key_id, public_key)
    missing = [p.value for p in Purpose if p not in found]
    if missing:
        raise CeremonyRefused(f"no intermediate for {', '.join(missing)}: every purpose of TABLE SEC-12a needs one")
    return found


def digest(path: Path) -> str:
    """What the custodians read aloud and write on the paper record: the sha256 of a ceremony file."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def issue(out: Path, *, root_key_id: str, root_public_key: str, root_sign: CommandSigner, intermediates: Sequence[str],
          not_before: str, root: str = "release", dry_run: bool = False,
          settings: Settings | None = None) -> dict[str, str]:  # fmt: skip
    """Issue the root's public record, the intermediate certificates and revocation list version 1, signing with the
    root through its command; each is checked before anything is written. Returns each written file's sha256."""
    settings = settings or Settings()
    if root not in ROOTS:
        raise CeremonyRefused(f"a ceremony makes the release or staging root, not {root!r} (OPS-35)")
    keys = _intermediates(intermediates)
    ids = [root_key_id, *(key_id for key_id, _ in keys.values())]
    if len(set(ids)) != len(ids) or any(k.startswith(("key_dev_", "key_root_test")) for k in ids):
        raise CeremonyRefused("every key id is new and none is a test hierarchy id")
    if any(k.startswith(DRY_RUN_PREFIX) for k in ids) != dry_run:
        raise CeremonyRefused(f"only a dry run uses {DRY_RUN_PREFIX} key ids, and a dry run uses only those")
    record = root_record(root_key_id, root_public_key, root, not_before,
                         _after_years(not_before, settings.signing_root_rotation_years), settings)  # fmt: skip
    certificates = []
    for purpose, (key_id, public_key) in keys.items():
        not_after = _after_years(not_before, int(getattr(settings, ROTATION_KEY[purpose])))
        certificates.append(
            issue_certificate(record, root_sign, key_id, purpose, public_key, not_before, not_after, settings)
        )
    store = TrustStore({record.key_id: record}, {c.key_id: c for c in certificates})
    problems = verify_issued(store)
    if problems:  # the command signed with another key than the root's public record names
        raise CeremonyRefused("; ".join(problems))
    revocations = sign_with_root(revocation_list(1, [], not_before), root_sign, record.key_id)
    try:
        verify_list(revocations, ROOTS[root], store)
    except SignatureRefused as error:
        raise CeremonyRefused(f"the revocation list does not verify: {error}") from error
    out.mkdir(parents=True, exist_ok=True)
    document = {"kind": KIND, "dry_run": dry_run, "created_at": _iso(datetime.now(UTC)), **store.document()}
    (out / RECORD).write_text(json.dumps(document, indent=1, sort_keys=True) + "\n")
    (out / REVOCATION).write_text(json.dumps(revocations, indent=1, sort_keys=True) + "\n")
    return {RECORD: digest(out / RECORD), REVOCATION: digest(out / REVOCATION)}


def adopt(record_path: Path, anchors: Path = ANCHORS) -> TrustStore:
    """Add a ceremony record's root and certificates to the bundled trust anchors, keeping the test hierarchy and every
    production root and certificate already there. A dry run's record is refused against the repository's anchors."""
    record = json.loads(record_path.read_text())
    if record.get("kind") != KIND:
        raise CeremonyRefused(f"{record_path} is not a key ceremony record")
    if record.get("dry_run") is True and anchors.resolve() == ANCHORS.resolve():
        raise CeremonyRefused("a dry run's throwaway keys never go into the repository's trust anchors")
    store = TrustStore.from_document(record)
    if not store.roots or any(r.root == TEST_ROOT for r in store.roots.values()):
        raise CeremonyRefused("a ceremony record holds a release or staging root")
    problems = verify_issued(store)
    if problems:
        raise CeremonyRefused("; ".join(problems))
    existing = production_anchors(anchors)
    clash = [k for k in [*store.roots, *store.certificates] if k in existing.roots or k in existing.certificates]
    if clash:
        raise CeremonyRefused(f"already in the anchors: {', '.join(sorted(clash))}; a new ceremony uses new key ids")
    anchors.write_text(anchors_text([existing, store]))
    return production_anchors(anchors)


def _sign_with_key(pem: Path) -> int:
    """Read bytes on stdin and print their base64 Ed25519 signature: the dry run's stand-in for an HSM's command."""
    key = load_pem_private_key(pem.read_bytes(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise SystemExit("not an Ed25519 private key")
    sys.stdout.write(base64.b64encode(key.sign(sys.stdin.buffer.read())).decode() + "\n")
    return 0


def _throwaway(folder: Path, name: str) -> tuple[str, list[str]]:
    """A throwaway key in `folder` (dry run only): its public key and the command that signs with it."""
    key = Ed25519PrivateKey.generate()
    pem = folder / f"{name}.pem"
    pem.write_bytes(key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()))
    pem.chmod(0o600)
    return public_b64(key), [sys.executable, "-m", "signing_service.ceremony", "sign-with-key", str(pem)]


# One document of every artefact type the dry run signs, by purpose (TABLE SEC-12a's "Signs" column).
SAMPLES: Mapping[Purpose, str] = {
    Purpose.CONTENT: "content_pack",
    Purpose.MODEL: "model_manifest",
    Purpose.AGENT_BUNDLE: "agent_bundle",
    Purpose.UPDATER: "speech_pack",
    Purpose.LICENCE: "licence_file",
}


def dry_run(out: Path | None = None, say: Any = print) -> Path:
    """Rehearse the whole ceremony with throwaway keys; returns the folder holding everything it made."""
    folder = out or Path(tempfile.mkdtemp(prefix="jana-key-ceremony-dry-run-"))
    keys = folder / "throwaway-private-keys"
    keys.mkdir(parents=True, exist_ok=True)
    keys.chmod(0o700)
    not_before = _iso(datetime.now(UTC).replace(microsecond=0))
    say(f"DRY RUN in {folder}: throwaway keys, never trusted by any build. Nothing in the repository changes.")
    root_public, root_command = _throwaway(keys, "root")
    say(f"1. Root key made (throwaway). Public key: {root_public}")
    commands: dict[Purpose, list[str]] = {}
    intermediates = []
    for purpose in Purpose:
        public, commands[purpose] = _throwaway(keys, purpose.value)
        intermediates.append(f"{purpose.value}={DRY_RUN_PREFIX}{purpose.value}={public}")
    say(f"2. Five intermediate keys made (throwaway): {', '.join(p.value for p in Purpose)}")
    sums = issue(folder / "ceremony", root_key_id=f"{DRY_RUN_PREFIX}root", root_public_key=root_public,
                 root_sign=CommandSigner(tuple(root_command)), intermediates=intermediates, not_before=not_before,
                 dry_run=True)  # fmt: skip
    say("3. Issued through the root's signing command, each certificate checked against the root:")
    for name, value in sums.items():
        say(f"   {name}  sha256 {value}")
    anchors = folder / "trust_anchors.json"
    shutil.copyfile(ANCHORS, anchors)
    store = adopt(folder / "ceremony" / RECORD, anchors)
    rewritten = anchors_text([production_anchors(anchors)])
    if rewritten != anchors.read_text():
        raise CeremonyRefused("the adopted anchors do not survive `anchors --write` unchanged")
    say(f"4. Adopted into a copy of the bundled anchors ({anchors}); `anchors --write` leaves it unchanged.")
    for purpose, artefact in SAMPLES.items():
        signer = production_signer(f"{DRY_RUN_PREFIX}{purpose.value}", commands[purpose], store)
        signed = signer.sign(artefact, {"kind": artefact, "id": f"dry-run-{artefact}"})
        store.verify(artefact, signed, Environment.PRODUCTION)
    say("5. Signed one " + ", one ".join(SAMPLES.values()) + " through each intermediate's command;"
        " a production build verifies every one.")  # fmt: skip
    wrong = production_signer(f"{DRY_RUN_PREFIX}content", commands[Purpose.CONTENT], store)
    try:
        wrong.sign("speech_pack", {"kind": "speech_pack"})
    except SigningRefused:
        say("6. A speech pack from the content key is refused at signing (D88): only the Updater key signs it.")
    else:
        raise CeremonyRefused("the content key signed a speech pack")
    if ARTEFACT_PURPOSE["speech_pack"] is not Purpose.UPDATER:
        raise CeremonyRefused("speech packs are no longer updater artefacts; update the guide")
    for line in rehearse(folder / "updates", keys):
        say(f"7. {line}")
    say("8. The real ceremony then runs, on the release machine (see KEY-CEREMONY.md):")
    say('   uv run python -m signing_service sign-speech-pack PACK_DIR --key-id <updater key id> --hsm-command "..."')
    say("   uv run python -m signing_service anchors --write")
    say("   and at each release, on this offline laptop, with the update folder the release job left unsigned:")
    say("   uv run python -m signing_service.ceremony sign-updates FOLDER --base-url <production https folder> \\")
    say('       --sign-command "cargo tauri signer sign --private-key-path /media/UPDATER/updater.key"')
    say(f"DRY RUN finished. Delete {folder} when you have read it; its keys are worthless but private.")
    return folder


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="signing_service.ceremony")
    p.add_argument("command", choices=["issue", "adopt", "dry-run", "sign-with-key", "sign-updates"])
    p.add_argument(
        "target",
        nargs="?",
        help="the ceremony record (adopt), the key PEM (sign-with-key) or the update folder (sign-updates)",
    )
    p.add_argument("--out")
    p.add_argument("--anchors", default=str(ANCHORS))
    p.add_argument("--root", default="release")
    p.add_argument("--root-key-id")
    p.add_argument("--root-public-key")
    p.add_argument("--root-sign-command")
    p.add_argument("--intermediate", action="append", default=[])
    p.add_argument("--not-before", default=None)
    p.add_argument("--sign-command", help="sign-updates: Tauri's signer, given each file's path last")
    p.add_argument("--pubkey", default=None, help="sign-updates: the updater public key (default: the app's)")
    p.add_argument("--base-url", help="sign-updates: the production https folder the bundles are served from")
    args = p.parse_args(argv)
    if args.command == "sign-updates":
        if not (args.target and args.sign_command and args.base_url):
            print("refused: sign-updates needs the folder, --sign-command and --base-url", file=sys.stderr)
            return 1
        return sign_main(Path(args.target), args.sign_command, args.pubkey, args.base_url)
    try:
        if args.command == "sign-with-key":
            return _sign_with_key(Path(args.target))
        if args.command == "dry-run":
            dry_run(Path(args.out) if args.out else None)
            return 0
        if args.command == "adopt":
            store = adopt(Path(args.target), Path(args.anchors))
            print(f"adopted: {len(store.roots)} production root(s), {len(store.certificates)} certificate(s) in "
                  f"{args.anchors}")  # fmt: skip
            return 0
        if not (args.out and args.root_key_id and args.root_public_key and args.root_sign_command):
            raise CeremonyRefused("issue needs --out, --root-key-id, --root-public-key and --root-sign-command")
        sums = issue(Path(args.out), root_key_id=args.root_key_id, root_public_key=args.root_public_key,
                     root_sign=CommandSigner(tuple(shlex.split(args.root_sign_command))),
                     intermediates=args.intermediate, root=args.root,
                     not_before=args.not_before or _iso(datetime.now(UTC).replace(microsecond=0)))  # fmt: skip
        print(json.dumps(sums, indent=1))
        return 0
    except (CeremonyRefused, SigningRefused) as error:
        print(f"refused: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
