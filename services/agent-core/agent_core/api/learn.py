"""The subject map and the lesson player over the local API (UXS-22, UXS-23): the map of one subject with each
KC's mastery state, its concept map (the skill backbone with the learner's states, questions and mistakes), one
lesson with its saved position, a quick check marked by the deterministic marker, and saving the position.

Every route acts for the profile whose learner database is open (the profile token when profiles exist, API-04); a
locked device answers `profile_token_expired` before anything is read (`ProfileScope`). The bodies are checked here
against the command contract's own definitions where it has them (`Answer`) and by shape where the proposal adds
them, since the contract has no definition to validate them against yet."""

from __future__ import annotations

import re
from typing import Any

from fastapi import FastAPI, Request, Response

from agent_core.api.core import CoreService
from agent_core.api.errors import ApiError, invalid
from agent_core.api.profiles import Mutating
from agent_core.engine.learn import CheckRefused

_CLIENT_ID = re.compile(r"^cli_[0-9A-HJKMNP-TV-Z]{26}$")
_SEMVER = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
_ITEM_ID = re.compile(r"^itm\.(mth|phy|chm|bio|acc)\.(o|a)\.[a-z0-9_]+\.[0-9]{4}$")
_KC_ID = re.compile(r"^kc\.(mth|phy|chm|bio|acc)\.(o|a)\.[a-z0-9_]+\.[a-z0-9_]+$")


def _check_body(body: Any) -> dict[str, Any]:
    """A `LessonCheck` body: the attempt's client id, the item, its answer (`Answer`), the time spent and, optionally,
    the student's try at the item from 1 (`attempt`, D110)."""
    required = {"client_attempt_id", "item_id", "answer", "time_ms"}
    if not isinstance(body, dict) or not required <= set(body) <= required | {"attempt"}:
        raise ApiError("validation_failed", "the body is {client_attempt_id, item_id, answer, time_ms, attempt?}")
    attempt = body.get("attempt", 1)
    if not isinstance(attempt, int) or isinstance(attempt, bool) or not 1 <= attempt <= 20:
        raise ApiError("validation_failed", "attempt is the try at the item, from 1 to 20")
    if not isinstance(body["client_attempt_id"], str) or not _CLIENT_ID.match(body["client_attempt_id"]):
        raise ApiError("validation_failed", "client_attempt_id is a cli_ id")
    if not isinstance(body["item_id"], str) or not _ITEM_ID.match(body["item_id"]):
        raise ApiError("validation_failed", "item_id is an item id")
    if not isinstance(body["time_ms"], int) or isinstance(body["time_ms"], bool) or body["time_ms"] < 0:
        raise ApiError("validation_failed", "time_ms is a whole number of milliseconds")
    refused = invalid("api/commands#/$defs/Answer", body["answer"])  # value-free (API-32)
    if refused is not None:
        raise refused
    return body


def _position_body(body: Any) -> tuple[int, str]:
    """A `LessonPositionSet` body: the block the student is on and the lesson version it belongs to."""
    if not isinstance(body, dict) or set(body) != {"block_index", "lesson_version"}:
        raise ApiError("validation_failed", "the body is {block_index, lesson_version}")
    index, version = body["block_index"], body["lesson_version"]
    if not isinstance(index, int) or isinstance(index, bool) or index < 0 or index > 99:
        raise ApiError("validation_failed", "block_index is a block number from 0")
    if not isinstance(version, str) or not _SEMVER.match(version):
        raise ApiError("validation_failed", "lesson_version is the lesson's version")
    return index, version


def add_routes(app: FastAPI, core: CoreService, mutating: Mutating) -> None:
    def desk() -> Any:
        if core.learn is None:
            raise ApiError("not_found", "lessons need the learner database")
        return core.learn()

    @app.get("/v1/curriculum/{subject_id}/map")
    async def subject_map(subject_id: str) -> dict[str, Any]:
        with desk() as opened:
            if opened is not None:
                opened.clock = core.clock_ms  # the states at now (SPEC §21.4)
            reply = opened.subject_map(subject_id) if opened is not None else None
        if reply is None:
            raise ApiError("not_found", "no such subject")
        return dict(reply)

    @app.get("/v1/curriculum/overview")
    async def overview() -> dict[str, Any]:
        """Every installed subject as topic tiles (SPEC §21.1); the read records the open on this device (JM-D45)."""
        with desk() as opened:
            if opened is None:
                raise ApiError("not_found", "the overview needs the learner database")
            opened.clock = core.clock_ms
            return dict(opened.overview())

    @app.get("/v1/curriculum/{subject_id}/concept-map")
    async def concept_map(subject_id: str, as_of: str | None = None) -> dict[str, Any]:
        """The subject's concept map; `?as_of=YYYY-MM-DD` replays it to that date, read-only (SPEC §21.3, JM-D37)."""
        from agent_core.engine.learner_map import AsOfRefused

        with desk() as opened:
            if opened is not None:
                opened.clock = core.clock_ms
            try:
                reply = opened.concept_map(subject_id, as_of) if opened is not None else None
            except AsOfRefused as error:
                raise ApiError("validation_failed", str(error)) from None
        if reply is None:
            raise ApiError("not_found", "no such subject")
        return dict(reply)

    @app.get("/v1/curriculum/kcs/{kc_id}/links")
    async def kc_links(kc_id: str) -> dict[str, Any]:
        """What is linked to one skill on this device (SPEC §21.3, JM-D38): read by the app, never a prompt."""
        if not _KC_ID.match(kc_id):
            raise ApiError("validation_failed", "kc_id is a knowledge component id")
        with desk() as opened:
            if opened is not None:
                opened.clock = core.clock_ms
            reply = opened.kc_links(kc_id) if opened is not None else None
        if reply is None:
            raise ApiError("not_found", "no such skill")
        return dict(reply)

    @app.get("/v1/lessons/{lesson_id}")
    async def lesson(lesson_id: str, teaching: bool = False) -> dict[str, Any]:
        core.assessment_guard()
        with desk() as opened:
            reply = opened.lesson(lesson_id, teaching=teaching) if opened is not None else None
        if reply is None:
            raise ApiError("not_found", "no such lesson")
        return dict(reply)

    @app.put("/v1/lessons/{lesson_id}/position")
    async def position(lesson_id: str, request: Request) -> Response:
        async def run(body: Any) -> tuple[int, Any]:
            index, version = _position_body(body)
            with desk() as opened:
                try:
                    saved = opened.save_position(lesson_id, index, version, core.clock_ms()) if opened else None
                except ValueError as error:
                    raise ApiError("validation_failed", str(error)) from None
            if saved is None:
                raise ApiError("not_found", "no such lesson")
            return 200, saved

        return await mutating(request, run)

    @app.post("/v1/lessons/{lesson_id}/checks")
    async def check(lesson_id: str, request: Request) -> Response:
        async def run(body: Any) -> tuple[int, Any]:
            core.assessment_guard()
            asked = _check_body(body)
            parts = asked["answer"]["parts"]
            with desk() as opened:
                try:
                    marked = (await opened.check(lesson_id, asked["item_id"], parts, asked.get("attempt", 1))
                              if opened is not None else None)  # fmt: skip
                except CheckRefused as error:
                    raise ApiError("validation_failed", str(error)) from None
            if marked is None:
                raise ApiError("not_found", "no such check in this lesson")
            return 200, {"client_attempt_id": asked["client_attempt_id"], **marked}

        return await mutating(request, run)
