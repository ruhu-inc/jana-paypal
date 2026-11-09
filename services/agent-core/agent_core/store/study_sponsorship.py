"""Eligibility for the opt-in hosted workshop. Read only identity, coverage and revision metadata."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

from agent_core.store import consents, subscription
from agent_core.store.profiles import internal_id


def eligible(root: Path, profile: str, source: str, revision: int, now_ms: int) -> bool:
    """The worker can recheck a volunteered place after sign-out without a profile token or reading its contents."""
    root = root.resolve()
    with closing(sqlite3.connect((root / "device.db").as_uri() + "?mode=ro", uri=True)) as device:
        installation = device.execute("SELECT install_kind FROM device").fetchone()
        if installation and installation[0] in ("school_lab", "school_server"):
            return False
        rows = device.execute("SELECT profile_id,db_file FROM profiles_index").fetchall()
        if len(rows) != 1 or rows[0][0] != internal_id(profile):
            return False
        if subscription.state(subscription.load(device), now_ms) in ("active", "pending_consent"):
            return False
        learner = (root / rows[0][1]).resolve()
        if not learner.is_relative_to(root.resolve()):
            return False
        with closing(sqlite3.connect(learner.as_uri() + "?mode=ro", uri=True)) as db:
            if db.execute("SELECT age_band FROM profile WHERE profile_id=?", (internal_id(profile),)).fetchone() != (
                "18_plus",
            ):
                return False
            if any(
                (r := consents.active(db, scope)) and r[5] is not None for scope in ("study_materials", "cloud_tier")
            ):
                return False
            return bool(
                db.execute(
                    "SELECT 1 FROM study_sources WHERE source_id=? AND current_revision=? AND private=0 "
                    "AND json_extract(body,'$.state')='ready' AND json_extract(body,'$.readable_units')>0 "
                    "AND NOT EXISTS (SELECT 1 FROM study_library_archive WHERE source_id=?) "
                    "AND NOT EXISTS (SELECT 1 FROM study_pending_revisions WHERE source_id=?)",
                    (source, revision, source, source),
                ).fetchone()
            )
