"""A fixed, fictional event-log fixture replayed through the learner's production projector."""

from __future__ import annotations

import math
import sqlite3
from typing import Any

from agent_core.learner.mastery import MasteryParams
from agent_core.learner.projections import Projector
from agent_core.learner.service import Learner
from agent_core.store.generated.schema import LEARNER
from agent_core.store.ids import new_ulid
from agent_core.store.pack_index import PackIndex
from agent_core.store.sqlite import connect, statements


def replay(pack: dict[str, Any], tables: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    conn = connect(":memory:")
    try:
        for sql in statements(LEARNER):
            conn.execute(sql)
        now = 1760000000000

        def clock() -> int:
            return now

        index = PackIndex.from_tables([(pack, tables)])
        learner = Learner.open(
            conn,
            profile_id=new_ulid(now),
            device_id="a" * 32,
            wall_ms=clock,
            max_skew_ms=86400000,
            params=MasteryParams(),
            content=index,
        )
        session = new_ulid(now)
        learner.record(
            "session_started",
            {"session_id": session, "mode": "practice", "entry": "question", "hardware_class": "mid"},
            source="engine",
            session_id=session,
        )
        for row in rows:
            for n, outcome in enumerate(("correct", "incorrect", "correct")):
                now += 1000
                attempt = new_ulid(now)
                learner.record(
                    "attempt",
                    {
                        "attempt_id": attempt,
                        "session_id": session,
                        "mode": "practice",
                        "item_id": "itm.phy.o.fixture.0001",
                        "problem_ref": f"fixture_{row['from_kc_id']}_{n}",
                        "step_index": 0,
                        "kc_ids": [{"kc_id": row["from_kc_id"], "weight": 1}],
                        "kc_source": "pack",
                        "attempt_index": 1,
                        "response_kind": "typed",
                        "normalised_response": "8",
                        "time_ms": 1000,
                    },
                    source="student",
                    session_id=session,
                )
                learner.record(
                    "verdict",
                    {
                        "verdict_id": new_ulid(now),
                        "attempt_id": attempt,
                        "outcome": outcome,
                        "check_ids": ["chk_fixture"],
                        "closes_opportunity": True,
                        "marker_version": "numeric.1",
                    },
                    source="checker",
                    session_id=session,
                )

        def snapshot(connection: sqlite3.Connection = conn) -> list[dict[str, Any]]:
            return [
                dict(zip(("kc_id", "succ_w", "fail_w", "attempts"), r, strict=True))
                for r in connection.execute("SELECT kc_id, succ_w, fail_w, attempts FROM kc_mastery ORDER BY kc_id")
            ]

        before = snapshot()
        migrated = {**tables, "kc_migrations": [*tables.get("kc_migrations", []), *rows]}
        projector = Projector(conn, MasteryParams(), PackIndex.from_tables([(pack, migrated)]), wall_ms=clock)
        projector.rebuild()
        after = snapshot()
        checksum = projector.checksum()
        projector.rebuild()
        events = [event.envelope() for event in learner.log.merged()]
        mapping = {row["from_kc_id"]: row["to"] for row in migrated["kc_migrations"]}

        def expand(identity: str, weight: float) -> list[dict[str, Any]]:
            if identity not in mapping:
                return [{"kc_id": identity, "weight": weight}]
            return [tag for target in mapping[identity] for tag in expand(target["kc_id"], weight * target["weight"])]

        for event in events:
            if event["type"] == "attempt":
                combined: dict[str, float] = {}
                for source_tag in event["payload"]["kc_ids"]:
                    for tag in expand(source_tag["kc_id"], source_tag["weight"]):
                        combined[tag["kc_id"]] = combined.get(tag["kc_id"], 0) + tag["weight"]
                event["payload"]["kc_ids"] = [{"kc_id": k, "weight": v} for k, v in combined.items()]
        reference_conn = connect(":memory:")
        try:
            for sql in statements(LEARNER):
                reference_conn.execute(sql)
            reference = Learner.open(
                reference_conn,
                profile_id=events[0]["profile_id"],
                device_id="b" * 32,
                wall_ms=clock,
                max_skew_ms=86400000,
                params=MasteryParams(),
                content=PackIndex.from_tables([(pack, {**tables, "kc_migrations": []})]),
            )
            reference.log.apply_remote(events)
            reference.projector.rebuild()
            expected = snapshot(reference_conn)
        finally:
            reference_conn.close()
        same = len(expected) == len(after) and all(
            a["kc_id"] == b["kc_id"]
            and all(math.isclose(a[k], b[k], abs_tol=1e-9) for k in ("succ_w", "fail_w", "attempts"))
            for a, b in zip(after, expected, strict=True)
        )
        return {
            "ok": same and checksum == projector.checksum(),
            "fixture": "fictional-three-opportunities-per-source-v1",
            "before": before,
            "after": after,
            "reference_after": expected,
            "reference_method": "Expand fixture tags independently; project without migrations",
            "rebuild_checksum": checksum,
        }
    finally:
        conn.close()
