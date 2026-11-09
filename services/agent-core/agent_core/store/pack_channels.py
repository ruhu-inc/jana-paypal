"""The two ways an update reaches the device (CUR-09, PLT-12): a download and the USB kit. There is no third (D52).

Both end with the same bytes in the same place, so the same diff installs identically whichever way it came:

- **Download** (online, optional, the `downloads` feature): the shell's download manager (PLT-11) fetches byte
  ranges in chunks into a `.part` file and resumes from its length after an interruption. `resume_download` is that
  assembly over any range reader; it moves the finished file into place by an atomic rename, after its sha256 matches
  when one is known.
- **USB kit**: a folder with a `kit.json` listing each file's path, size and sha256, signed by a content key
  (TABLE SEC-12a) and shaped by the `ops/usb-kit` contract (D39). `from_kit` verifies the listing's signature and
  shape, then the file's size and hash, then copies it into place the same way.

Whatever the channel, `PackStore.apply_diff` then verifies the diff's own signature and the resulting pack. With no
channel available the installed version keeps working: nothing here runs unless an update is offered.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from collections.abc import Callable
from pathlib import Path

from agent_core.config.environments import Environment
from agent_core.config.trust import SignatureRefused, TrustStore, bundled_trust
from agent_core.contracts import errors as contract_errors
from agent_core.contracts.ops.usb_kit import KitFile, USBKitListing

KIT_FILE = "kit.json"
KIT_ARTEFACT = "usb_kit"
KIT_CONTRACT = "ops/usb-kit"
KIT_FORMAT = "jana.usb-kit.1"

# Reads `length` bytes from `offset`; returns fewer (or none) when the connection drops.
RangeReader = Callable[[int, int], bytes]


class ChannelError(ValueError):
    """The file did not arrive whole, or the kit listing does not verify."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _place(source: Path, target: Path) -> Path:
    temp = target.with_name(target.name + ".tmp")
    shutil.copyfile(source, temp)
    with temp.open("rb") as handle:
        os.fsync(handle.fileno())
    os.replace(temp, target)
    return target


def resume_download(read: RangeReader, total: int, target: Path, chunk_bytes: int,
                    sha256: str | None = None) -> Path | None:  # fmt: skip
    """Continue (or start) a download into `target`; returns it when complete, None when the connection dropped (the
    bytes so far stay in `<target>.part` for the next attempt)."""
    part = target.with_name(target.name + ".part")
    target.parent.mkdir(parents=True, exist_ok=True)
    offset = part.stat().st_size if part.is_file() else 0
    if offset > total:
        part.unlink()
        offset = 0
    with part.open("ab") as handle:
        while offset < total:
            data = read(offset, min(chunk_bytes, total - offset))
            if not data:
                handle.flush()
                os.fsync(handle.fileno())
                return None
            handle.write(data)
            offset += len(data)
        handle.flush()
        os.fsync(handle.fileno())
    if sha256 is not None and _sha256(part) != sha256:
        part.unlink()
        raise ChannelError(f"{target.name}: the download does not match its sha256; it starts again")
    os.replace(part, target)
    return target


def kit_listing(kit_dir: Path, environment: Environment, trust: TrustStore | None = None) -> USBKitListing:
    """The kit's signed `kit.json`, verified (a content key valid when the kit was made) and read through its
    contract (D39): a listing that does not match it is refused like a bad signature."""
    try:
        listing = json.loads((kit_dir / KIT_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ChannelError(f"{kit_dir}: no readable {KIT_FILE}") from error
    if not isinstance(listing, dict):
        raise ChannelError(f"{KIT_FILE}: not a kit listing")
    created = listing.get("created_at")
    try:
        (trust or bundled_trust()).verify(KIT_ARTEFACT, listing, environment,
                                          signed_at=created if isinstance(created, str) else None)  # fmt: skip
    except SignatureRefused as error:
        raise ChannelError(f"{KIT_FILE}: {error}") from error
    problems = contract_errors(KIT_CONTRACT, listing)
    if problems:
        raise ChannelError(f"{KIT_FILE}: does not match the kit contract ({problems[0][:200]})")
    return USBKitListing.model_validate(listing)


def from_kit(kit_dir: Path, name: str, target: Path, environment: Environment,
             trust: TrustStore | None = None) -> Path:  # fmt: skip
    """Copy the kit file `name` into `target` after the signed listing vouches for its size and sha256."""
    listing = kit_listing(kit_dir, environment, trust)
    entry = next((f for f in listing.files if f.path == name), None)
    if entry is None:
        raise ChannelError(f"{name} is not in the kit")
    source = (kit_dir / name).resolve()
    if kit_dir.resolve() not in source.parents or not source.is_file():
        raise ChannelError(f"{name}: not a file inside the kit")
    if source.stat().st_size != entry.size_bytes or _sha256(source) != entry.sha256:
        raise ChannelError(f"{name}: the file does not match the kit listing")
    target.parent.mkdir(parents=True, exist_ok=True)
    return _place(source, target)


def kit_entry(kit_dir: Path, name: str, kind: str) -> KitFile:
    """A listing entry for a file already in the kit folder (what the signing service writes before signing); a kind
    outside the contract's list is refused (pydantic's ValidationError, a ValueError)."""
    path = kit_dir / name
    return KitFile.model_validate({"path": name, "kind": kind, "size_bytes": path.stat().st_size,
                                   "sha256": _sha256(path)})  # fmt: skip
