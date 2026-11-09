"""python -m signing_service process [--inbox DIR] [--out DIR] [--repo DIR]: sign every queued publish request.
python -m signing_service anchors [--write]: print (or write) the public trust anchors the app bundles (SEC-12).
python -m signing_service safety-sample PACK_DIR [--out DIR] --role ROLE: write the unsigned content safety review of
  the pack's next release (CUR-27): its sample against the last release under --out, every record `approved`; the
  reviewer edits any decision, then signs.
python -m signing_service safety-sign REVIEW --key PEM --key-id ID: sign a review with the reviewer's own Ed25519 key
  (a PEM file the reviewer holds; never one in the repository).
python -m signing_service sign-speech-pack PACK_DIR --key-id ID --hsm-command "CMD ...": sign a speech pack in place
  with the production Updater intermediate through its HSM command (D88), after the pack passes its own gate.
python -m signing_service sign-speech-pack PACK_DIR --dev: the same with the test hierarchy's updater key, trusted by
  development builds only."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from agent_core.config.trust import ANCHORS
from pack_builder.accounts import home

from signing_service.hierarchy import anchors_text, production_anchors
from signing_service.service import process_inbox


def _safety_sample(pack_dir: Path, out: Path, role: str) -> Path:
    from agent_core.config import Settings
    from pack_builder import safety_review, scan
    from pack_builder.accounts import now_iso
    from pack_builder.build import load_source

    from signing_service.service import _released_tables

    source = load_source(pack_dir)
    releases = out / "releases" / str(source.pack["pack_id"])
    latest = releases / "latest.json"
    previous = json.loads(latest.read_text()) if latest.is_file() else None
    lists = scan.lists_for(pack_dir)
    if lists is None:
        raise SystemExit("no content scan lists beside the pack")
    wanted = safety_review.sample(source.pack, source.tables, _released_tables(releases, previous), lists, Settings())
    path = safety_review.review_path(pack_dir.parent.parent, wanted.pack_id, wanted.version)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(wanted.unsigned(role, now_iso()), indent=1, sort_keys=True) + "\n")
    return path


def _safety_sign(review: Path, key_file: Path, key_id: str) -> None:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import load_pem_private_key

    from signing_service.safety_review import sign_review

    key = load_pem_private_key(key_file.read_bytes(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise SystemExit("the reviewer key must be an Ed25519 private key")
    signed = sign_review(json.loads(review.read_text()), key, key_id)
    review.write_text(json.dumps(signed, indent=1, sort_keys=True) + "\n")


def _sign_speech_pack(folder: Path, dev: bool, key_id: str | None, command: str | None) -> dict[str, object]:
    import shlex

    from agent_core.config import Environment
    from agent_core.config.trust import Purpose

    from signing_service.hierarchy import dev_signer, dev_trust, production_signer
    from signing_service.speech import sign_speech_pack

    if dev:
        signed = sign_speech_pack(folder, dev_signer(Purpose.UPDATER), dev_trust(), Environment.DEV)
    else:
        if not key_id or not command:
            raise SystemExit("production signing needs --key-id and --hsm-command (or --dev for development builds)")
        anchors = production_anchors()
        signer = production_signer(key_id, shlex.split(command), anchors)
        if signer.purpose is not Purpose.UPDATER:
            raise SystemExit(f"{key_id} is a {signer.purpose.value} key; a speech pack needs the Updater key (D88)")
        signed = sign_speech_pack(folder, signer, anchors, Environment.PRODUCTION)
    return {
        "speech_pack_id": signed["speech_pack_id"],
        "created_at": signed["created_at"],
        "key_id": signed["signature"]["key_id"],
        "files": len(signed["files"]),
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="signing_service")
    p.add_argument("command", choices=["process", "anchors", "safety-sample", "safety-sign", "sign-speech-pack"])
    p.add_argument("target", nargs="?", help="the pack directory (safety-sample, sign-speech-pack) or review file")
    p.add_argument("--role", default="rev_safety_reviewer")
    p.add_argument("--key")
    p.add_argument("--key-id")
    p.add_argument("--hsm-command", help="sign-speech-pack: the HSM signing command (bytes on stdin, base64 out)")
    p.add_argument("--dev", action="store_true", help="sign-speech-pack: the test hierarchy (development builds)")
    p.add_argument("--inbox", default=str(home() / "signing" / "inbox"))
    p.add_argument("--out", default=str(home() / "signing" / "out"))
    p.add_argument("--repo", default=str(Path(__file__).resolve().parents[3]))
    p.add_argument("--write", action="store_true")
    args = p.parse_args(argv)
    if args.command == "safety-sample":
        print(_safety_sample(Path(args.target), Path(args.out), args.role))
        return 0
    if args.command == "safety-sign":
        _safety_sign(Path(args.target), Path(args.key), args.key_id)
        return 0
    if args.command == "sign-speech-pack":
        if not args.target:
            raise SystemExit("name the speech pack folder to sign")
        print(json.dumps(_sign_speech_pack(Path(args.target).expanduser(), args.dev, args.key_id, args.hsm_command)))
        return 0
    if args.command == "anchors":
        text = anchors_text([production_anchors()])
        if args.write:
            ANCHORS.write_text(text)
        else:
            print(text, end="")
        return 0
    outcomes = process_inbox(Path(args.inbox), Path(args.repo), Path(args.out))
    print(json.dumps(outcomes, indent=1))
    return 1 if any(o["status"] != "signed" for o in outcomes) else 0


if __name__ == "__main__":
    sys.exit(main())
