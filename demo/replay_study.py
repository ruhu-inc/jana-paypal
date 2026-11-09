import copy, json
from pathlib import Path
from agent_core.engine.study import StudyDesk
from agent_core.store.profiles import internal_id
from agent_core.store.sqlite import open_database
from contextlib import closing
import sqlite3, time

record = json.loads(Path(__file__).with_name("chemistry.json").read_text())
ws = record["worksheet"]


def import_archive(directory, profile, source, settings):
    with closing(sqlite3.connect((directory / "device.db").resolve().as_uri() + "?mode=ro", uri=True)) as device:
        relative = device.execute(
            "select db_file from profiles_index where profile_id=?", (internal_id(profile),)
        ).fetchone()[0]
    conn, _ = open_database(directory / relative, "learner")
    with closing(conn):
        d = StudyDesk(conn, lambda: int(time.time() * 1000), settings)
        refs = [{"source_id": source, "revision": 1}]
        (chunk,) = d.selected(refs)
        assert chunk["text"] == ws
        for kind in ("chat", "quiz"):
            row = next(v for v in record["cases"] if v["kind"] == kind)
            response = copy.deepcopy(row["response"])
            req = {"kind": kind, "sources": refs, "execution": "prefer_cloud"}
            if kind == "chat":
                req["prompt"] = row["prompt"]
            job = d.create_job(req)
            job["state"] = "running"
            d.save_job(job)

            def attach(o):
                if isinstance(o, list):
                    for v in o:
                        attach(v)
                if not isinstance(o, dict):
                    return
                if "quote" in o and "chunk_id" in o:
                    assert chunk["text"][o["start"] : o["end"]] == o["quote"]
                    o.update(source_id=source, chunk_id=chunk["chunk_id"])
                for v in o.values():
                    attach(v)

            attach(response)
            response.update(request_id=job["job_id"], model=record["model"])
            n = len(response["claims"]) + len(response.get("quiz", {}).get("items", []))
            d.commit_aid(job, response, [True] * n)
            print("Imported verified saved", kind, "into disposable profile.", flush=True)
