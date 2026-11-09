"""Starting crash capture for the core process (OBS-16): the crash store under the device key, the hooks, and the
records other processes and earlier runs left (the shell's panic inbox, fatal errors), pruned to their retention."""

from __future__ import annotations

from pathlib import Path

from agent_core.config import Settings
from agent_core.store.keystore import KeyStore
from agent_core.trace.crashes import CrashStore, device_key
from agent_core.trace.logs import diagnostic_logging

__all__ = ["diagnostic_logging", "start_crash_capture"]


def start_crash_capture(data_dir: Path, keys: KeyStore, app_version: str, settings: Settings) -> CrashStore:
    """`keys` holds the device key (SEC-17): the shell's keychain bridge in the app, a memory store in tests."""
    store = CrashStore(data_dir / "crashes", device_key(keys), app_version, settings)
    store.install_hooks("core")
    store.ingest_inbox()
    store.ingest_faults()
    store.prune()
    return store
