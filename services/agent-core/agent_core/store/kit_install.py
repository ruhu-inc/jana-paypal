"""Installing from the USB kit or a folder (PLT-12, Settings → "Install from USB or folder").

The kit's signed `kit.json` lists every file with its size and sha256 (`pack_channels.kit_listing`). Content packs are
listed as `content_pack` files, model packs as `model_pack_file` entries under `models/<model_pack_id>/` and speech
packs (VOC-03, from S3) as `speech_pack_file` entries under `speech/<speech_pack_id>/`. Each file
is copied out only after the listing vouches for it, then installed exactly as a downloaded one would be: the content
pack store, the model pack store and the speech pack store verify signatures and hashes again before anything becomes
active. The kit carries
the revocation list current when it was made (SEC-16), adopted when newer. Nothing here touches the network.
"""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from agent_core.config.environments import Environment
from agent_core.config.revocation import RevocationStore
from agent_core.config.trust import TrustStore
from agent_core.contracts.ops.usb_kit import USBKitListing
from agent_core.store.model_pack_store import ModelPackError, ModelPackStore
from agent_core.store.pack_channels import ChannelError, from_kit, kit_listing
from agent_core.store.pack_store import PackStore
from agent_core.store.packs import PackRejectedError
from agent_core.store.speech_packs import PACK_ID as SPEECH_PACK_ID
from agent_core.store.speech_packs import SpeechPackError, SpeechPackStore

CONTENT = "content_pack"
MODEL_FILE = "model_pack_file"
SPEECH_FILE = "speech_pack_file"

Progress = Callable[[str, str], None]  # (item id, state): verifying, installing, done, failed


@dataclass
class KitReport:
    content: list[str] = field(default_factory=list)
    models: list[str] = field(default_factory=list)
    speech: list[str] = field(default_factory=list)
    refused: list[tuple[str, str]] = field(default_factory=list)  # (item, message key)


def kit_items(listing: USBKitListing) -> tuple[list[str], dict[str, list[str]]]:
    """The content pack files and the model packs (id -> files) a kit lists."""
    content = [f.path for f in listing.files if f.kind == CONTENT]
    models: dict[str, list[str]] = {}
    for entry in listing.files:
        parts = entry.path.split("/")
        if entry.kind == MODEL_FILE and len(parts) == 3 and parts[0] == "models":
            models.setdefault(parts[1], []).append(entry.path)
    return content, models


def kit_speech_packs(listing: USBKitListing) -> dict[str, list[str]]:
    """The speech packs (id -> files, nested paths kept) a kit lists."""
    packs: dict[str, list[str]] = {}
    for entry in listing.files:
        parts = entry.path.split("/")
        nested = len(parts) >= 3 and parts[0] == "speech" and SPEECH_PACK_ID.match(parts[1])
        if entry.kind == SPEECH_FILE and nested:
            packs.setdefault(parts[1], []).append(entry.path)
    return packs


def install_kit(kit_dir: Path, packs: PackStore, models: ModelPackStore | None, environment: Environment, *,
                trust: TrustStore | None = None, revocations: RevocationStore | None = None,
                only: Iterable[str] | None = None, progress: Progress | None = None,
                speech: SpeechPackStore | None = None) -> KitReport:  # fmt: skip
    """Install every content pack, model pack and speech pack in the kit (or only the ids in `only`). One refused item
    never stops the others; what was already installed stays as it was."""
    listing = kit_listing(kit_dir, environment, trust)
    if revocations is not None and listing.revocation_list is not None:
        revocations.adopt(listing.revocation_list.model_dump(mode="json"))
    wanted = set(only) if only is not None else None
    report = KitReport()
    content, model_files = kit_items(listing)

    def say(item: str, state: str) -> None:
        if progress is not None:
            progress(item, state)

    with tempfile.TemporaryDirectory(dir=packs.folder) as work:
        scratch = Path(work)
        for name in content:
            item = Path(name).stem
            if wanted is not None and item not in wanted:
                continue
            say(item, "verifying")
            try:
                copied = from_kit(kit_dir, name, scratch / Path(name).name, environment, trust)
                say(item, "installing")
                manifest = packs.install(copied)
            except (ChannelError, PackRejectedError):
                report.refused.append((item, "pack.damaged"))
                say(item, "failed")
                continue
            report.content.append(str(manifest["pack_id"]))
            say(item, "done")
        for model_pack_id, files in sorted(model_files.items()):
            if models is None or (wanted is not None and model_pack_id not in wanted):
                continue
            say(model_pack_id, "verifying")
            folder = scratch / "models" / model_pack_id
            try:
                for name in files:
                    from_kit(kit_dir, name, folder / Path(name).name, environment, trust)
                say(model_pack_id, "installing")
                models.install(folder)
            except ChannelError:
                report.refused.append((model_pack_id, "model.damaged"))
                say(model_pack_id, "failed")
                continue
            except ModelPackError as error:
                report.refused.append((model_pack_id, error.code))
                say(model_pack_id, "failed")
                continue
            finally:
                shutil.rmtree(folder, ignore_errors=True)
            report.models.append(model_pack_id)
            say(model_pack_id, "done")
        for speech_pack_id, files in sorted(kit_speech_packs(listing).items()):
            if speech is None or (wanted is not None and speech_pack_id not in wanted):
                continue
            say(speech_pack_id, "verifying")
            folder = scratch / "speech" / speech_pack_id
            try:
                for name in files:
                    from_kit(kit_dir, name, folder.joinpath(*name.split("/")[2:]), environment, trust)
                say(speech_pack_id, "installing")
                speech.install(folder)
            except ChannelError:
                report.refused.append((speech_pack_id, "speech.damaged"))
                say(speech_pack_id, "failed")
                continue
            except SpeechPackError as error:
                report.refused.append((speech_pack_id, str(error)))
                say(speech_pack_id, "failed")
                continue
            finally:
                shutil.rmtree(folder, ignore_errors=True)
            report.speech.append(speech_pack_id)
            say(speech_pack_id, "done")
    return report
