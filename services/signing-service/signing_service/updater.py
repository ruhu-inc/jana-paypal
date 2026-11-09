"""App updates signed offline (SEC-12, SEC-15, PLT-14; owner ruling 2026-10-17: no updater key in CI secrets).

Tauri's updater checks its own signature format (minisign: Ed25519 over the BLAKE2b-512 of the file, with a signed
trusted comment) against the public key in `apps/desktop/src-tauri/tauri.updater.conf.json`. Its private key never
reaches CI: the release job builds the update bundles unsigned and writes a request listing them; the release manager
carries the folder to the offline ceremony laptop, where `sign` checks every file against the request, has the
updater key sign each one through a signing command, verifies each signature against the public key and writes the
signed result; publishing (`jana_tools release publish --kind app_update`) takes only an artefact that result signs.

    python -m signing_service.updater request BUNDLE_DIR --os windows|macos|linux --version V --out FOLDER   (CI)
    python -m signing_service.ceremony sign-updates FOLDER --sign-command "CMD ..." [--pubkey KEY] --base-url URL
    python -m signing_service.updater verify FOLDER [--pubkey KEY]                                     (publishing)
    python -m signing_service.updater sign-with-key PEM FILE     (the rehearsal's stand-in for Tauri's signer)

The signing command gets the file's path as its last argument and leaves the signature beside it as `FILE.sig`, as
`cargo tauri signer sign --private-key-path KEY FILE` does; the tool never sees the private key or its password.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import shlex
import shutil
import subprocess
import sys
import tarfile
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent_core.config import Settings
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat, load_pem_private_key

REPO_ROOT = Path(__file__).resolve().parents[3]
UPDATER_CONFIG = REPO_ROOT / "apps" / "desktop" / "src-tauri" / "tauri.updater.conf.json"
REQUEST_KIND = "jana_update_signing_request"
SIGNED_KIND = "jana_update_signatures"
SIGNED = "update-signatures.json"
LATEST = "latest.json"
PLACEHOLDER = "PLACEHOLDER"
# Tauri's platform keys for each OS's update bundle (TABLE PLT-01a: x64 Windows and Linux, Apple silicon Macs).
TARGETS: Mapping[str, tuple[tuple[str, str], ...]] = {
    "windows": (("windows-x86_64-nsis", "nsis/*-setup.exe"), ("windows-x86_64-msi", "msi/*.msi")),
    "macos": (("darwin-aarch64-app", "macos/*.app"),),
    "linux": (("linux-x86_64-appimage", "appimage/*.AppImage"),),
}


class UpdateSigningRefused(ValueError):
    """An update bundle, request, signature or key that must not be published."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- minisign, as Tauri writes and reads it ---------------------------------------------------------------------


def _key_lines(text: str, what: str) -> list[str]:
    try:
        decoded = base64.b64decode(text.strip(), validate=True).decode()
    except (ValueError, UnicodeDecodeError) as error:
        raise UpdateSigningRefused(f"the {what} is not a Tauri {what}") from error
    return [line.strip() for line in decoded.strip().splitlines()]


def public_key(text: str) -> tuple[bytes, Ed25519PublicKey]:
    """(key number, key) of a Tauri updater public key (base64 of a minisign public key file)."""
    lines = _key_lines(text, "public key")
    try:
        raw = base64.b64decode(lines[1], validate=True)
    except (IndexError, ValueError) as error:
        raise UpdateSigningRefused("the public key is not a Tauri public key") from error
    if len(raw) != 42 or raw[:2] != b"Ed":
        raise UpdateSigningRefused("the public key is not an Ed25519 minisign key")
    return raw[2:10], Ed25519PublicKey.from_public_bytes(raw[10:])


def verify_signature(data_path: Path, signature: str, key_text: str) -> str:
    """Check a Tauri `.sig` (base64 of a minisign signature) of `data_path` against the public key, the trusted
    comment included; returns the trusted comment. Raises UpdateSigningRefused on any mismatch."""
    keynum, key = public_key(key_text)
    lines = _key_lines(signature, "signature")
    try:
        raw = base64.b64decode(lines[1], validate=True)
        comment = lines[2].removeprefix("trusted comment: ")
        global_signature = base64.b64decode(lines[3], validate=True)
        if not lines[2].startswith("trusted comment: "):
            raise ValueError("no trusted comment")
    except (IndexError, ValueError) as error:
        raise UpdateSigningRefused(f"{data_path.name}.sig is not a minisign signature") from error
    if len(raw) != 74 or raw[:2] not in (b"ED", b"Ed"):
        raise UpdateSigningRefused(f"{data_path.name}.sig is not an Ed25519 signature")
    if raw[2:10] != keynum:
        raise UpdateSigningRefused(f"{data_path.name} is signed by another key than the updater public key")
    data = data_path.read_bytes()
    message = hashlib.blake2b(data, digest_size=64).digest() if raw[:2] == b"ED" else data
    try:
        key.verify(raw[10:], message)
        key.verify(global_signature, raw[10:] + comment.encode())
    except InvalidSignature as error:
        raise UpdateSigningRefused(f"{data_path.name}: the signature does not verify") from error
    return comment


def tauri_public_key(key: Ed25519PrivateKey, keynum: bytes) -> str:
    """A key's Tauri public key text, for rehearsals and tests (the real one: `cargo tauri signer generate`)."""
    raw = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    body = f"untrusted comment: minisign public key: {keynum[::-1].hex().upper()}\n"
    body += base64.b64encode(b"Ed" + keynum + raw).decode() + "\n"
    return base64.b64encode(body.encode()).decode()


def sign_with_key(key: Ed25519PrivateKey, keynum: bytes, path: Path) -> Path:
    """Write `path.sig` as Tauri's signer does (rehearsals and tests only)."""
    signature = key.sign(hashlib.blake2b(path.read_bytes(), digest_size=64).digest())
    comment = f"timestamp:{int(datetime.now(UTC).timestamp())}\tfile:{path.name}"
    text = "untrusted comment: signature from tauri secret key\n"
    text += base64.b64encode(b"ED" + keynum + signature).decode() + "\n"
    text += f"trusted comment: {comment}\n"
    text += base64.b64encode(key.sign(signature + comment.encode())).decode() + "\n"
    out = path.with_name(path.name + ".sig")
    out.write_text(base64.b64encode(text.encode()).decode())
    return out


def configured_public_key(config: Path = UPDATER_CONFIG) -> str:
    """The updater public key the app ships (`plugins.updater.pubkey`); refused while it is the placeholder."""
    key = str(json.loads(config.read_text()).get("plugins", {}).get("updater", {}).get("pubkey", ""))
    if not key or key.startswith(PLACEHOLDER):
        raise UpdateSigningRefused(f"{config.name} has no updater public key yet: run the key ceremony's step 8")
    public_key(key)
    return key


# --- CI: the unsigned bundles and the request ---------------------------------------------------------------------


def _tar_app(app: Path, out: Path) -> Path:
    """The macOS update bundle Tauri's updater installs: the .app, gzipped, at the archive's root."""
    target = out / f"{app.name}.tar.gz"
    with tarfile.open(target, "w:gz") as archive:
        archive.add(app, arcname=app.name)
    return target


def request(bundle: Path, os_name: str, version: str, out: Path) -> Path:
    """CI: copy this OS's update bundles from Tauri's bundle folder into `out`, unsigned, with the request that lists
    each one's target, sha256 and size. Returns the request file."""
    if os_name not in TARGETS:
        raise UpdateSigningRefused(f"unknown OS {os_name!r}")
    out.mkdir(parents=True, exist_ok=True)
    artefacts = []
    for target, pattern in TARGETS[os_name]:
        found = sorted(bundle.glob(pattern))
        if len(found) != 1:
            raise UpdateSigningRefused(f"want one {pattern} in {bundle}, found {len(found)}")
        source = found[0]
        if source.suffix == ".app":
            copied = _tar_app(source, out)
        else:
            copied = out / source.name
            shutil.copyfile(source, copied)
        if (out / f"{copied.name}.sig").exists():
            raise UpdateSigningRefused(f"{copied.name} already has a signature: CI never signs app updates")
        artefacts.append({"target": target, "file": copied.name, "sha256": _sha256(copied),
                          "size_bytes": copied.stat().st_size})  # fmt: skip
    document = {"kind": REQUEST_KIND, "os": os_name, "version": version, "created_at": _now(),
                "artefacts": artefacts}  # fmt: skip
    path = out / f"update-request-{os_name}.json"
    path.write_text(json.dumps(document, indent=1, sort_keys=True) + "\n")
    return path


def _requests(folder: Path) -> tuple[str, list[dict[str, Any]]]:
    found = sorted(folder.glob("update-request-*.json"))
    if not found:
        raise UpdateSigningRefused(f"no update-request-*.json in {folder}")
    versions, artefacts = set(), []
    for path in found:
        document = json.loads(path.read_text())
        if document.get("kind") != REQUEST_KIND:
            raise UpdateSigningRefused(f"{path.name} is not an update signing request")
        versions.add(str(document["version"]))
        artefacts += document["artefacts"]
    if len(versions) != 1:
        raise UpdateSigningRefused(f"the requests name different versions: {sorted(versions)}")
    names = [a["file"] for a in artefacts]
    if len(set(names)) != len(names) or any(Path(n).name != n for n in names):
        raise UpdateSigningRefused("two artefacts share a file name, or a name is a path")
    return versions.pop(), artefacts


def _check_files(folder: Path, artefacts: Sequence[Mapping[str, Any]]) -> None:
    for artefact in artefacts:
        path = folder / artefact["file"]
        if not path.is_file():
            raise UpdateSigningRefused(f"{artefact['file']} is missing")
        if path.stat().st_size != artefact["size_bytes"] or _sha256(path) != artefact["sha256"]:
            raise UpdateSigningRefused(f"{artefact['file']} is not the file CI built (its sha256 differs)")


# --- the offline laptop: sign, verify, write the signed result -----------------------------------------------------


def command_signer(command: Sequence[str]) -> Callable[[Path], None]:
    """A signing command that takes the file's path last and writes `FILE.sig` beside it."""

    def sign(path: Path) -> None:
        # The command may wait for the key's password at the laptop's keyboard, within its configured time.
        limit = Settings().updater_sign_command_timeout_s
        done = subprocess.run([*command, str(path)], check=False, timeout=limit)
        if done.returncode != 0:
            raise UpdateSigningRefused(f"the signing command failed on {path.name} (exit {done.returncode})")

    return sign


def sign(folder: Path, signer: Callable[[Path], None], key_text: str, base_url: str) -> dict[str, Any]:
    """Sign every update bundle the requests in `folder` list, after checking each is the file CI built; verify each
    signature against the updater public key; write the signed result (`update-signatures.json`) and Tauri's
    `latest.json`. Nothing is written when any step is refused."""
    version, artefacts = _requests(folder)
    public_key(key_text)
    if not base_url.startswith("https://") or PLACEHOLDER in base_url:
        raise UpdateSigningRefused("the download base URL must be the production https host")
    _check_files(folder, artefacts)
    signed = []
    for artefact in artefacts:
        path = folder / artefact["file"]
        sig = path.with_name(path.name + ".sig")
        sig.unlink(missing_ok=True)
        signer(path)
        if not sig.is_file():
            raise UpdateSigningRefused(f"the signing command left no {sig.name}")
        signature = sig.read_text().strip()
        verify_signature(path, signature, key_text)
        signed.append({**artefact, "signature": signature})
    _check_files(folder, artefacts)  # the command changed no file it signed
    pub_date = _now()
    result = {"kind": SIGNED_KIND, "version": version, "pub_date": pub_date, "public_key": key_text,
              "artefacts": signed}  # fmt: skip
    base = base_url.rstrip("/")
    platforms = {a["target"]: {"signature": a["signature"], "url": f"{base}/{a['file']}"} for a in signed}
    latest = {"version": version, "pub_date": pub_date, "platforms": platforms}
    (folder / SIGNED).write_text(json.dumps(result, indent=1, sort_keys=True) + "\n")
    (folder / LATEST).write_text(json.dumps(latest, indent=1, sort_keys=True) + "\n")
    return result


def verify(folder: Path, key_text: str) -> dict[str, Any]:
    """Publishing's check: the signed result covers every requested bundle, each file is the one CI built and each
    signature verifies against the updater public key the app ships."""
    path = folder / SIGNED
    if not path.is_file():
        raise UpdateSigningRefused(f"no {SIGNED} in {folder}: sign the update on the ceremony laptop first")
    result: dict[str, Any] = json.loads(path.read_text())
    version, artefacts = _requests(folder)
    if result.get("kind") != SIGNED_KIND or result.get("version") != version:
        raise UpdateSigningRefused(f"{SIGNED} does not sign version {version}")
    if result.get("public_key") != key_text:
        raise UpdateSigningRefused(f"{SIGNED} was signed for another updater public key")
    by_file = {a["file"]: a for a in result["artefacts"]}
    _check_files(folder, artefacts)
    for artefact in artefacts:
        entry = by_file.get(artefact["file"])
        if entry is None or entry["sha256"] != artefact["sha256"]:
            raise UpdateSigningRefused(f"{artefact['file']} is not in the signed result")
        verify_signature(folder / artefact["file"], entry["signature"], key_text)
    return result


def signed_entry(signatures: Path, artefact: Path, key_text: str) -> dict[str, Any]:
    """The signed result's entry for one artefact about to be published, its signature checked."""
    result = verify(signatures.parent, key_text)
    sha = _sha256(artefact)
    entry = next((a for a in result["artefacts"] if a["sha256"] == sha), None)
    if entry is None:
        raise UpdateSigningRefused(f"{artefact.name} is not an update bundle the offline signing covered")
    verify_signature(artefact, entry["signature"], key_text)
    return dict(entry)


def _sign_with_pem(pem: Path, path: Path) -> int:
    key = load_pem_private_key(pem.read_bytes(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise SystemExit("not an Ed25519 private key")
    keynum = hashlib.sha256(key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).digest()[:8]
    sign_with_key(key, keynum, path)
    return 0


def pem_public_key(pem: Path) -> str:
    """The Tauri public key text of a rehearsal PEM, with the key number `sign-with-key` uses."""
    key = load_pem_private_key(pem.read_bytes(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise UpdateSigningRefused("not an Ed25519 private key")
    keynum = hashlib.sha256(key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).digest()[:8]
    return tauri_public_key(key, keynum)


def rehearse(folder: Path, key_folder: Path) -> list[str]:
    """The key ceremony's dry run of `sign-updates`: a made-up Windows update bundle, its request, signing through the
    stand-in command with a throwaway key, the publishing check, and a changed bundle refused. Returns what it did."""
    from cryptography.hazmat.primitives.serialization import NoEncryption, PrivateFormat

    pem = key_folder / "updater-throwaway.pem"
    pem.write_bytes(Ed25519PrivateKey.generate().private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()))
    pem.chmod(0o600)
    bundle = folder / "bundle"
    for name in ("nsis/Jana_0.0.0_x64-setup.exe", "msi/Jana_0.0.0_x64_en-US.msi"):
        (bundle / name).parent.mkdir(parents=True, exist_ok=True)
        (bundle / name).write_bytes(b"MZ made-up update bundle for the rehearsal " + name.encode())
    out = folder / "update"
    request(bundle, "windows", "0.0.0", out)
    key = pem_public_key(pem)
    stand_in = command_signer((sys.executable, "-m", "signing_service.updater", "sign-with-key", str(pem)))
    sign(out, stand_in, key, "https://updates.example.invalid/v1/files")
    verify(out, key)
    changed = out / "Jana_0.0.0_x64-setup.exe"
    changed.write_bytes(changed.read_bytes() + b"!")
    try:
        verify(out, key)
    except UpdateSigningRefused:
        pass
    else:
        raise UpdateSigningRefused("a changed update bundle passed the publishing check")
    return [
        "Update bundles: a made-up Windows request was signed through the stand-in command, verified for publishing,"
        " and a changed bundle was refused.",
    ]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="signing_service.updater")
    p.add_argument("command", choices=["request", "verify", "sign-with-key"])
    p.add_argument("target", help="the bundle folder (request), the signed folder (verify) or the PEM (sign-with-key)")
    p.add_argument("file", nargs="?", help="the file to sign (sign-with-key)")
    p.add_argument("--os", dest="os_name")
    p.add_argument("--version")
    p.add_argument("--out")
    p.add_argument("--pubkey", default=None)
    args = p.parse_args(argv)
    try:
        if args.command == "sign-with-key":
            return _sign_with_pem(Path(args.target), Path(str(args.file)))
        if args.command == "request":
            if not (args.os_name and args.version and args.out):
                raise UpdateSigningRefused("request needs --os, --version and --out")
            print(request(Path(args.target), args.os_name, args.version, Path(args.out)))
            return 0
        result = verify(Path(args.target), args.pubkey or configured_public_key())
        print(f"verified: version {result['version']}, {len(result['artefacts'])} signed update bundle(s)")
        return 0
    except UpdateSigningRefused as error:
        print(f"refused: {error}", file=sys.stderr)
        return 1


def sign_main(folder: Path, sign_command: str, pubkey: str | None, base_url: str) -> int:
    """`ceremony sign-updates`: sign on the offline laptop and print what to carry back."""
    try:
        key = pubkey or configured_public_key()
        result = sign(folder, command_signer(shlex.split(sign_command)), key, base_url)
    except UpdateSigningRefused as error:
        print(f"refused: {error}", file=sys.stderr)
        return 1
    for artefact in result["artefacts"]:
        print(f"signed {artefact['file']}  sha256 {artefact['sha256']}")
    print(f"wrote {SIGNED} (sha256 {_sha256(folder / SIGNED)}) and {LATEST}; carry the folder back for publishing")
    return 0


if __name__ == "__main__":
    sys.exit(main())
