"""The hosted demo: the browser app on one server, one isolated core per judge, behind private links.

Each private link belongs to a slot with its own data folder and its own core process, so judges never see each other's
profiles. This process is the only thing a browser reaches: it serves the built app, hands a slot's launch details
only to the browser holding that slot's cookie, and forwards `/core/*` to that slot's core on loopback. The core and
the gateway keep their loopback guards unchanged, and no provider key reaches this process or a core.

    python -m jana_tools.hosted serve --config /etc/jana/hosted.toml
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import hashlib
import hmac
import html
import json
import logging
import os
import re
import secrets
import subprocess
import sys
import time
import tomllib
import urllib.parse
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import uvicorn
import websockets
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from jana_tools import REPO_ROOT
from jana_tools.hosted_accounts import (
    Account,
    AccountError,
    Accounts,
    Attempts,
    hash_password,
    normalise_email,
    verify_password,
)
from jana_tools.model_dev import environment as browser_environment

log = logging.getLogger("jana.hosted")

COOKIE = "jana_slot"
SESSION_COOKIE = "jana_session"
STATE_COOKIE = "jana_google_state"
MAX_FORM_BYTES = 8192
GOOGLE_AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN = "https://oauth2.googleapis.com/token"  # noqa: S105 -- a URL
GOOGLE_ISSUERS = ("https://accounts.google.com", "accounts.google.com")
GOOGLE_STATE_SECONDS = 600
COOKIE_DAYS = 14
SLOT_NAME = re.compile(r"^[a-z0-9-]{1,32}$")
MIN_SECRET_LENGTH = 24
# Connection-level headers never cross a proxy (RFC 9110 §7.6.1). Host is set to the loopback the core's guard expects.
HOP_BY_HOP = frozenset({"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailer",
                        "transfer-encoding", "upgrade", "host", "content-length", "cookie"})  # fmt: skip
SEND_STARTED = {"accept-ranges", "cache-control", "content-disposition", "content-language", "content-range",
                "content-type", "etag", "last-modified", "vary", "x-accel-buffering"}  # fmt: skip
SECURITY_HEADERS = {
    "x-content-type-options": "nosniff",
    "referrer-policy": "no-referrer",
    "x-frame-options": "DENY",
}


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Config:
    public_origin: str
    bundle: Path
    data_root: Path
    gateway_config: str
    gateway_port: int
    secure_cookie: bool
    slots: Mapping[str, str]  # slot name to the SHA-256 of its link secret; the secret itself is never stored
    warm: bool = False
    member_slots: int = 0  # slots for signed-in accounts, beside the private links' slots
    accounts_db: Path | None = None
    invite_digest: str | None = None  # the SHA-256 of the sign-up code; no digest means sign-up is closed
    google_client_id: str | None = None
    google_secret_file: Path | None = None

    sponsored_file: Path | None = None
    workshop_setup_slots: tuple[str, ...] = ()
    workshop_setup_anchors: Path | None = None

    @property
    def members(self) -> list[str]:
        return [f"member-{i + 1}" for i in range(self.member_slots)]

    @property
    def all_slots(self) -> list[str]:
        return [*self.slots, *self.members]


def load(path: Path) -> Config:
    raw = tomllib.loads(path.read_text())
    origin = str(raw.get("public_origin", "")).rstrip("/")
    if not re.fullmatch(r"https?://[a-z0-9.:\-\[\]]+", origin):
        raise ConfigError("public_origin must be a bare origin such as https://demo.example.com")
    slots = raw.get("slots")
    if not isinstance(slots, dict) or not slots:
        raise ConfigError("at least one slot is needed")
    for name, digest in slots.items():
        if not SLOT_NAME.fullmatch(name):
            raise ConfigError(f"bad slot name {name!r}")
        if not re.fullmatch(r"[0-9a-f]{64}", str(digest)):
            raise ConfigError(f"slot {name} must hold the SHA-256 of its secret, not the secret")
    invite = raw.get("invite_digest")
    if invite is not None and not re.fullmatch(r"[0-9a-f]{64}", str(invite)):
        raise ConfigError("invite_digest must hold the SHA-256 of the code, not the code")
    gateway = raw.get("gateway_config")
    if not isinstance(gateway, str) or not gateway.strip():
        raise ConfigError("Set gateway_config to the gateway TOML file for this deployment")
    return Config(
        public_origin=origin,
        bundle=Path(raw["bundle"]),
        data_root=Path(raw["data_root"]),
        gateway_config=gateway,
        gateway_port=int(raw.get("gateway_port", 8081)),
        secure_cookie=origin.startswith("https://"),
        slots=dict(slots),
        warm=bool(raw.get("warm", False)),
        member_slots=int(raw.get("member_slots", 0)),
        accounts_db=Path(raw["accounts_db"]) if raw.get("accounts_db") else None,
        invite_digest=invite,
        workshop_setup_slots=tuple(raw.get("workshop_setup_slots", [])),
        workshop_setup_anchors=Path(raw["workshop_setup_anchors"]) if raw.get("workshop_setup_anchors") else None,
        sponsored_file=Path(raw["sponsored_file"]) if raw.get("sponsored_file") else None,
        google_client_id=raw.get("google_client_id"),
        google_secret_file=Path(raw["google_secret_file"]) if raw.get("google_secret_file") else None,
    )


def digest(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def slot_for(config: Config, secret: str | None) -> str | None:
    """The slot a link secret or cookie belongs to. Every slot is compared, so timing reveals nothing."""
    if not secret or len(secret) < MIN_SECRET_LENGTH:
        return None
    presented = digest(secret)
    found = None
    for name, expected in config.slots.items():
        if hmac.compare_digest(presented, expected):
            found = name
    return found


def core_environment(config: Config, data_dir: Path, base: Mapping[str, str]) -> dict[str, str]:
    """Start the core with this deployment's gateway and its private profile data."""
    env = browser_environment(base, Path(config.gateway_config), data_dir, 0, config.gateway_port)
    for name in list(env):
        if "PAYPAL" in name.upper() or name in (
            "CREDENTIALS_DIRECTORY",
            "JANA_SPONSORED_ANCHORS",
            "JANA_ADULT_WORKSHOP",
        ):
            env.pop(name)
    return env


def control_version() -> tuple[int, int]:
    versions = (REPO_ROOT / "scripts" / "jana_tools" / "codegen" / "versions.toml").read_text()
    found = re.search(r'\[interfaces\.control\][\s\S]*?^version\s*=\s*"(\d+)\.(\d+)"', versions, re.M)
    if found is None:
        raise ConfigError("control protocol version is missing")
    return int(found[1]), int(found[2])


@dataclass
class Core:
    """One slot's core process, started the way the desktop shell starts it (ARC-04): the token goes over stdin."""

    name: str
    config: Config
    token: str = field(default_factory=lambda: secrets.token_urlsafe(32))
    port: int | None = None
    sponsored_anchors: Path | None = None
    adult_workshop: bool = False
    process: asyncio.subprocess.Process | None = None
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _reader: asyncio.Task[None] | None = None

    @property
    def data_dir(self) -> Path:
        return self.config.data_root / self.name

    def running(self) -> bool:
        return self.port is not None and self.process is not None and self.process.returncode is None

    async def ensure(self) -> None:
        async with self._lock:
            if self.running():
                return
            self.port = None
            self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            env = core_environment(self.config, self.data_dir, os.environ)
            if self.sponsored_anchors:
                env["JANA_SPONSORED_ANCHORS"] = str(self.sponsored_anchors)
                if self.adult_workshop:
                    env["JANA_ADULT_WORKSHOP"] = "1"
            process = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "jana_tools.hosted_core",
                cwd=REPO_ROOT,
                env=env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                limit=1024 * 1024,
            )  # fmt: skip
            self.process = process
            major, minor = control_version()
            for message in (
                {"type": "control.hello", "protocol": {"major": major, "minor": minor}},
                {
                    "type": "control.config",
                    "data_dir": str(self.data_dir),
                    "content_root": str(REPO_ROOT / "content" / "agent"),
                    "skills_root": str(REPO_ROOT / "skills"),
                    "release": False,
                    "dev_origins": [self.config.public_origin],
                    "scripted": False,
                },
                {"type": "control.secret", "name": "launch_token", "value": self.token},
            ):
                self._send(message)
            await process.stdin.drain()  # type: ignore[union-attr]
            started: asyncio.Future[int] = asyncio.get_running_loop().create_future()
            self._reader = asyncio.create_task(self._read(process, started))
            self.port = await asyncio.wait_for(started, timeout=180)
            log.warning("slot %s: core ready", self.name)

    def _send(self, message: dict[str, Any]) -> None:
        if self.process is not None and self.process.stdin is not None:
            self.process.stdin.write((json.dumps(message) + "\n").encode())

    async def _read(self, process: asyncio.subprocess.Process, started: asyncio.Future[int]) -> None:
        assert process.stdout is not None
        async for raw in process.stdout:
            try:
                message = json.loads(raw)
            except ValueError:
                continue
            kind = message.get("type")
            if kind == "control.ready" and not started.done():
                started.set_result(int(message["api_port"]))
            elif kind == "control.error":
                log.error("slot %s: core refused to start: %s", self.name, message.get("code"))
                if not started.done():
                    started.set_exception(RuntimeError(str(message.get("code"))))
            elif kind == "control.voice_start":
                # On-device speech packs are a desktop feature; hosted voice goes through the gateway.
                self._send({"type": "control.voice_failed", "request_id": message.get("request_id"),
                            "code": "voice_start_failed"})  # fmt: skip
        if not started.done():
            started.set_exception(RuntimeError(f"core exited {await process.wait()}"))

    async def stop(self) -> None:
        if self.process is None or self.process.returncode is not None:
            return
        self._send({"type": "control.shutdown"})
        try:
            await asyncio.wait_for(self.process.wait(), timeout=15)
        except TimeoutError:
            self.process.kill()


LANDING = (Path(__file__).parent / "hosted_landing.html").read_text()
SIGNIN_PAGE = (Path(__file__).parent / "hosted_signin.html").read_text()
SIGNIN_ERRORS = {
    "wrong": "That email and password don't match.",
    "slow": "Too many tries. Please wait a few minutes and try again.",
    "invite": "That invite code isn't right.",
    "email_taken": "There's already an account with that email. Sign in instead.",
    "no_space": "All the demo places are taken. Please contact the team.",
    "invalid_email": "Please enter a valid email address.",
    "weak_password": "Please choose a password of at least 10 characters.",
    "google": "Google sign-in didn't complete. Please try again.",
    "google_invite": "To create an account with Google, enter your invite code on the sign-up page first.",
    "closed": "Sign-up is closed.",
}

REFUSED = (
    '<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Jana</title></head>'
    '<body style="font:16px system-ui;padding:40px">This demo opens from a private link. '
    "Please use the link you were given.</body></html>"
)


def _page(body: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(
        SIGNIN_PAGE.replace("{body}", body),
        status_code=status,
        headers={**SECURITY_HEADERS, "referrer-policy": "same-origin", "cache-control": "no-store"},
    )


def _error(code: str | None) -> str:
    return f'<p class="bad" role="alert">{html.escape(SIGNIN_ERRORS[code])}</p>' if code in SIGNIN_ERRORS else ""


def signin_page(error: str | None = None, google: bool = False, status: int = 200) -> HTMLResponse:
    with_google = '<a class="google" href="/auth/google">Continue with Google</a>' if google else ""
    return _page(
        '<h1>Sign in</h1><p>Sign in to study with Jana.</p><form method="post" action="/signin">'
        '<label for="email">Email</label><input id="email" name="email" type="email" autocomplete="email" required>'
        '<label for="password">Password</label>'
        '<input id="password" name="password" type="password" autocomplete="current-password" required>'
        f'<button type="submit">Sign in</button></form>{with_google}{_error(error)}'
        '<p class="foot">New here? <a href="/signup">Create an account</a> with your invite code.</p>'
        '<p class="foot">Forgot your password? Please contact the team to reset it.</p>',
        status,
    )


def signup_page(error: str | None = None, google: bool = False, status: int = 200) -> HTMLResponse:
    with_google = (
        '<button class="quiet" type="submit" formaction="/auth/google/start" formnovalidate>'
        "Continue with Google</button>"
        if google
        else ""
    )
    return _page(
        '<h1>Create an account</h1><p>Sign-up needs an invite code.</p><form method="post" action="/signup">'
        '<label for="invite">Invite code</label><input id="invite" name="invite" autocomplete="off" required>'
        '<label for="email">Email</label><input id="email" name="email" type="email" autocomplete="email">'
        '<label for="password">Password, at least 10 characters</label>'
        '<input id="password" name="password" type="password" autocomplete="new-password" minlength="10">'
        f'<button type="submit">Create account</button>{with_google}</form>{_error(error)}'
        '<p class="foot">Already have an account? <a href="/signin">Sign in</a></p>',
        status,
    )


async def warm(cores: Mapping[str, Core]) -> None:
    """Start every judge's core ahead of time, one by one, so a first visit does not wait for one to start."""
    for name, core in cores.items():
        try:
            await core.ensure()
        except Exception:  # a first visit tries again and reports it
            log.exception("slot %s: core did not start ahead of time", name)


def create_app(config: Config, google_transport: httpx.AsyncBaseTransport | None = None) -> Starlette:
    cores = {name: Core(name, config) for name in config.all_slots}
    client = httpx.AsyncClient(trust_env=False, timeout=httpx.Timeout(None, connect=10))
    bundle = config.bundle.resolve()
    accounts = Accounts(config.accounts_db, config.members) if config.accounts_db is not None else None
    if config.workshop_setup_slots:
        if (
            not config.workshop_setup_anchors
            or set(config.workshop_setup_slots) - set(config.members)
            or not config.secure_cookie
        ):
            raise ConfigError("Workshop setup requires HTTPS, named member slots and dedicated development anchors")
        for slot in config.workshop_setup_slots:
            cores[slot].sponsored_anchors = config.workshop_setup_anchors
    workshop = None
    if config.sponsored_file:
        from jana_tools.hosted_sponsorship import Workshop

        workshop = Workshop(config, accounts, cores, client)
    sharing = (
        "For online Study, you choose whether to send worksheet excerpts to Google Vertex AI (Gemini 3.8 Flash)."
        if workshop
        else "You choose whether to share material for online Study."
    )
    landing = LANDING.replace("{study_sharing}", sharing)
    google_on = config.google_client_id is not None and config.google_secret_file is not None
    sign_ins, sign_ups = Attempts(8, 900), Attempts(5, 3600)
    google_states: dict[str, tuple[float, str | None]] = {}

    def slot_of(request: Request | WebSocket) -> str | None:
        linked = slot_for(config, request.cookies.get(COOKIE))
        if linked is not None or accounts is None:
            return linked
        account = accounts.from_session(request.cookies.get(SESSION_COOKIE))
        return account.slot if account is not None else None

    def visitor(request: Request) -> str:
        # Caddy, the only thing that can reach this process, replaces this header with the real client address.
        return request.headers.get("x-forwarded-for", "local").split(",")[0].strip()

    async def form(request: Request) -> dict[str, str] | None:
        """A posted form, or None when it comes from another site or is too large."""
        if request.headers.get("origin") not in (None, config.public_origin):
            return None
        body = await request.body()
        if len(body) > MAX_FORM_BYTES:
            return None
        parsed = urllib.parse.parse_qs(body.decode(errors="replace"), keep_blank_values=True)
        return {key: values[0] for key, values in parsed.items()}

    def signed_in(account: Account, target: str = "/welcome") -> Response:
        assert accounts is not None
        response = RedirectResponse(target, status_code=303, headers={"cache-control": "no-store"})
        response.set_cookie(
            SESSION_COOKIE, accounts.start_session(account.account_id), max_age=COOKIE_DAYS * 86400,
            httponly=True, secure=config.secure_cookie, samesite="lax",
        )  # fmt: skip
        return response

    def invite_ok(code: str | None) -> bool:
        if config.invite_digest is None or not code:
            return False
        return hmac.compare_digest(digest(code.strip()), config.invite_digest)

    async def signin(request: Request) -> Response:
        if accounts is None:
            return refused()
        if request.method == "GET":
            return signin_page(google=google_on)
        fields = await form(request)
        if fields is None:
            return refused()
        address, password = fields.get("email", "").strip().lower(), fields.get("password", "")
        if not sign_ins.allow(f"ip:{visitor(request)}") or not sign_ins.allow(f"email:{address}"):
            return signin_page("slow", google_on, 429)
        stored = accounts.password_hash(address)
        if not await asyncio.to_thread(verify_password, password, stored):
            return signin_page("wrong", google_on, 401)
        account = accounts.by_email(address)
        assert account is not None
        return signed_in(account, "/")

    async def signup(request: Request) -> Response:
        if accounts is None:
            return refused()
        if request.method == "GET":
            return signup_page(None if config.invite_digest else "closed", google_on)
        fields = await form(request)
        if fields is None:
            return refused()
        if not sign_ups.allow(f"ip:{visitor(request)}"):
            return signup_page("slow", google_on, 429)
        if not invite_ok(fields.get("invite")):
            return signup_page("invite", google_on, 403)
        try:
            password_hash = await asyncio.to_thread(hash_password, fields.get("password", ""))
            account = accounts.create(fields.get("email", ""), password_hash)
        except AccountError as error:
            return signup_page(str(error), google_on, 400)
        log.warning("account created in %s", account.slot)
        return signed_in(account)

    def go_to_google(invite: str | None) -> Response:
        state = secrets.token_urlsafe(24)
        now = time.monotonic()
        for key in [k for k, (at, _) in google_states.items() if now - at > GOOGLE_STATE_SECONDS]:
            del google_states[key]
        google_states[state] = (now, invite)
        query = urllib.parse.urlencode({
            "client_id": config.google_client_id, "redirect_uri": f"{config.public_origin}/auth/google/callback",
            "response_type": "code", "scope": "openid email", "state": state, "prompt": "select_account",
        })  # fmt: skip
        response = RedirectResponse(f"{GOOGLE_AUTH}?{query}", status_code=303)
        response.set_cookie(STATE_COOKIE, state, max_age=GOOGLE_STATE_SECONDS, httponly=True,
                            secure=config.secure_cookie, samesite="lax")  # fmt: skip
        return response

    async def google_signin(_request: Request) -> Response:
        return go_to_google(None) if google_on and accounts is not None else refused()

    async def google_signup(request: Request) -> Response:
        if not google_on or accounts is None:
            return refused()
        fields = await form(request)
        if fields is None:
            return refused()
        if not sign_ups.allow(f"ip:{visitor(request)}"):
            return signup_page("slow", google_on, 429)
        if not invite_ok(fields.get("invite")):
            return signup_page("invite", google_on, 403)
        return go_to_google(fields.get("invite"))

    async def google_identity(code: str) -> tuple[str, str] | None:
        """The verified address and stable id from Google's token endpoint, reached directly over TLS."""
        assert config.google_secret_file is not None
        async with httpx.AsyncClient(trust_env=False, timeout=15, transport=google_transport) as caller:
            reply = await caller.post(GOOGLE_TOKEN, data={
                "code": code, "client_id": config.google_client_id,
                "client_secret": config.google_secret_file.read_text().strip(),
                "redirect_uri": f"{config.public_origin}/auth/google/callback", "grant_type": "authorization_code",
            })  # fmt: skip
        if reply.status_code != 200:
            return None
        try:
            part = reply.json()["id_token"].split(".")[1]
            claims = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
        except (KeyError, ValueError, IndexError, AttributeError):
            return None
        if (
            claims.get("aud") != config.google_client_id
            or claims.get("iss") not in GOOGLE_ISSUERS
            or int(claims.get("exp", 0)) < time.time()
            or claims.get("email_verified") not in (True, "true")
            or not claims.get("sub")
            or not claims.get("email")
        ):
            return None
        return str(claims["email"]), str(claims["sub"])

    async def google_callback(request: Request) -> Response:
        if not google_on or accounts is None:
            return refused()
        state = request.query_params.get("state", "")
        held = google_states.pop(state, None)
        cookie = request.cookies.get(STATE_COOKIE, "")
        if held is None or not hmac.compare_digest(state, cookie) or time.monotonic() - held[0] > GOOGLE_STATE_SECONDS:
            return signin_page("google", google_on, 400)
        code = request.query_params.get("code")
        identity = await google_identity(code) if code else None
        if identity is None:
            return signin_page("google", google_on, 400)
        address, sub = identity
        account = accounts.by_google(sub)
        if account is None:
            account = accounts.by_email(address)
            if account is not None:  # Google has proved this address, so it joins the existing account
                accounts.link_google(account.account_id, sub)
        target = "/"
        if account is None:
            if not invite_ok(held[1]):
                return signin_page("google_invite", google_on, 403)
            try:
                normalise_email(address)
                account = accounts.create(address, None, sub)
            except AccountError as error:
                return signin_page(str(error), google_on, 400)
            target = "/welcome"
        response = signed_in(account, target)
        response.delete_cookie(STATE_COOKIE)
        return response

    async def welcome(request: Request) -> Response:
        if slot_of(request) is None:
            return RedirectResponse("/signin", status_code=303)
        return HTMLResponse(landing, headers={**SECURITY_HEADERS, "cache-control": "no-store"})

    async def account_page(request: Request) -> Response:
        account = accounts.from_session(request.cookies.get(SESSION_COOKIE)) if accounts is not None else None
        if account is None:
            return RedirectResponse("/signin", status_code=303)
        return _page(
            f"<h1>Your account</h1><p>Signed in as {html.escape(account.email)}.</p>"
            '<form method="post" action="/signout"><button type="submit">Sign out</button></form>'
            '<p class="foot"><a href="/">Back to Jana</a></p>'
        )

    async def signout(request: Request) -> Response:
        if await form(request) is None:
            return refused()
        if accounts is not None:
            accounts.end_session(request.cookies.get(SESSION_COOKIE))
        response = RedirectResponse("/signin", status_code=303)
        response.delete_cookie(SESSION_COOKIE)
        return response

    def to_signin() -> Response:
        return RedirectResponse("/signin", status_code=303) if accounts is not None else refused()

    def refused() -> HTMLResponse:
        return HTMLResponse(REFUSED, status_code=403, headers=SECURITY_HEADERS)

    async def link(request: Request) -> Response:
        secret = request.path_params["secret"]
        if slot_for(config, secret) is None:
            return refused()
        response = HTMLResponse(landing, headers={**SECURITY_HEADERS, "cache-control": "no-store"})
        response.set_cookie(
            COOKIE, secret, max_age=COOKIE_DAYS * 86400, httponly=True, secure=config.secure_cookie, samesite="lax"
        )
        return response

    async def launch(request: Request) -> Response:
        name = slot_of(request)
        if name is None:
            return JSONResponse({"error": "no_link"}, status_code=403)
        core = cores[name]
        try:
            await core.ensure()
        except Exception:
            log.exception("slot %s: core failed to start", name)
            return JSONResponse({"error": "core_failed"}, status_code=503, headers={"cache-control": "no-store"})
        body = {"base_url": f"{config.public_origin}/core", "token": core.token}
        if workshop and name in workshop.members and not request.cookies.get(COOKIE):
            body["sponsored_study"] = True
        return JSONResponse(body, headers={"cache-control": "no-store"})

    async def proxy(request: Request) -> Response:
        name = slot_of(request)
        if name is None:
            return JSONResponse({"error": "no_link"}, status_code=403)
        if (
            workshop
            and name in workshop.members
            and request.method == "POST"
            and request.path_params["path"].rstrip("/") == "v1/subscription"
        ):
            # All signed coverage writes for this personal core share the claim/settlement lock.
            async with workshop.lock:
                member = workshop.members[name]
                if workshop.pay.member(workshop.wid, member.profile)["state"] in ("claimed", "committed", "funded"):
                    try:
                        body = await request.json()
                    except ValueError:
                        return Response(status_code=400)
                    if isinstance(body, dict) and body.get("entitlement"):
                        return JSONResponse(
                            {"error": {"code": "request_conflict", "retryable": False}}, status_code=409
                        )
                return await forward(request, name)
        return await forward(request, name)

    async def forward(request: Request, name: str) -> Response:
        core = cores[name]
        if not core.running():
            return JSONResponse({"error": "core_not_running"}, status_code=503)
        headers = [(k, v) for k, v in request.headers.items() if k.lower() not in HOP_BY_HOP]
        headers.append(("host", f"127.0.0.1:{core.port}"))
        upstream = client.build_request(
            request.method,
            f"http://127.0.0.1:{core.port}/{request.path_params['path']}",
            params=request.url.query.encode() or None,
            headers=headers,
            content=request.stream() if request.method not in ("GET", "HEAD") else None,
        )
        try:
            reply = await client.send(upstream, stream=True)
        except httpx.HTTPError:
            log.warning("slot %s: core did not answer", name)
            return JSONResponse({"error": "core_unreachable"}, status_code=502)

        async def body() -> AsyncIterator[bytes]:
            try:
                async for chunk in reply.aiter_raw():
                    yield chunk
            finally:
                await reply.aclose()

        passed = {k: v for k, v in reply.headers.items() if k.lower() in SEND_STARTED or k.lower().startswith("x-jana")}
        if "content-encoding" in reply.headers:
            passed["content-encoding"] = reply.headers["content-encoding"]
        passed["x-accel-buffering"] = "no"  # streamed answers must reach the page as they are made
        return StreamingResponse(body(), status_code=reply.status_code, headers=passed)

    async def socket(websocket: WebSocket) -> None:
        name = slot_of(websocket)
        if name is None or not cores[name].running():
            await websocket.close(code=4403)
            return
        core = cores[name]
        offered = list(websocket.scope.get("subprotocols", []))
        origin = websocket.headers.get("origin")
        try:
            async with websockets.connect(
                f"ws://127.0.0.1:{core.port}/{websocket.path_params['path']}",
                subprotocols=offered,
                origin=origin,  # type: ignore[arg-type]
                max_size=None,
                proxy=None,
            ) as upstream:
                await websocket.accept(subprotocol=upstream.subprotocol)

                async def up() -> None:
                    while True:
                        message = await websocket.receive()
                        if message["type"] == "websocket.disconnect":
                            return
                        await upstream.send(message["bytes"] if message.get("bytes") is not None else message["text"])

                async def down() -> None:
                    async for message in upstream:
                        if isinstance(message, bytes):
                            await websocket.send_bytes(message)
                        else:
                            await websocket.send_text(message)

                tasks = [asyncio.create_task(up()), asyncio.create_task(down())]
                await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in tasks:
                    task.cancel()
        except (websockets.WebSocketException, OSError, WebSocketDisconnect):
            log.warning("slot %s: a voice connection closed", name)
        finally:
            with contextlib.suppress(RuntimeError):
                await websocket.close()

    async def asset(request: Request) -> Response:
        if slot_of(request) is None:
            return refused() if request.path_params["path"].startswith("assets/") else to_signin()
        wanted = (bundle / request.path_params["path"]).resolve()
        if wanted.is_file() and wanted.is_relative_to(bundle):
            return FileResponse(wanted, headers=SECURITY_HEADERS)
        return FileResponse(bundle / "index.html", headers={**SECURITY_HEADERS, "cache-control": "no-store"})

    async def healthy(_request: Request) -> Response:
        return JSONResponse({"ok": True})

    async def home(request: Request) -> Response:
        if slot_of(request) is None:
            return to_signin()
        return FileResponse(bundle / "index.html", headers={**SECURITY_HEADERS, "cache-control": "no-store"})

    @contextlib.asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        # Only the private links' slots: a member slot starts on its first visit, so idle places cost no memory.
        warming = asyncio.create_task(warm({name: cores[name] for name in config.slots})) if config.warm else None
        settlement = asyncio.create_task(workshop.poll()) if workshop else None
        yield
        if settlement is not None:
            settlement.cancel()
            await asyncio.gather(settlement, return_exceptions=True)
        if warming is not None:
            warming.cancel()
        await asyncio.gather(*(core.stop() for core in cores.values()), return_exceptions=True)
        await client.aclose()

    methods = ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]
    app = Starlette(
        routes=[
            Route("/healthz", healthy),
            Route("/j/{secret}", link),
            Route("/signin", signin, methods=["GET", "POST"]),
            Route("/signup", signup, methods=["GET", "POST"]),
            Route("/signout", signout, methods=["POST"]),
            Route("/account", account_page),
            Route("/welcome", welcome),
            Route("/auth/google", google_signin),
            Route("/auth/google/start", google_signup, methods=["POST"]),
            Route("/auth/google/callback", google_callback),
            Route("/__jana_dev/launch", launch),
            Route("/core/{path:path}", proxy, methods=methods),
            WebSocketRoute("/core/{path:path}", socket),
            Route("/", home),
            Route("/{path:path}", asset),
        ],
        lifespan=lifespan,
    )
    if workshop:
        app.router.routes = workshop.routes(app.router.routes)
    app.state.cores = cores
    return app


def new_invite() -> tuple[str, str]:
    """A sign-up code people can type, and the digest the config holds."""
    code = "-".join(secrets.token_hex(3) for _ in range(3))
    return code, digest(code)


def new_links(names: list[str], origin: str) -> tuple[dict[str, str], dict[str, str]]:
    """Fresh secrets: the digests go in the config, and the links are shown once and stored nowhere on the server."""
    digests, links = {}, {}
    for name in names:
        secret = secrets.token_urlsafe(24)
        digests[name] = digest(secret)
        links[name] = f"{origin.rstrip('/')}/j/{secret}"
    return digests, links


def prepare(config: Config) -> None:
    """Seed packs into fresh slots without copying another participant's profile."""
    for name in config.all_slots:
        target = config.data_root / name
        if target.exists():
            print(f"{name}: already prepared")
            continue
        target.mkdir(parents=True, mode=0o700)
        subprocess.run([sys.executable, str(REPO_ROOT / "apps" / "desktop" / "dev" / "seed_packs.py"), str(target)],
                       cwd=REPO_ROOT, check=True)  # fmt: skip
        print(f"{name}: prepared")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="Run the front door")
    serve.add_argument("--config", type=Path, required=True)
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    links = commands.add_parser("links", help="Make new private links: digests for the config, links for you")
    links.add_argument("--origin", required=True)
    links.add_argument("--count", type=int, default=10)
    commands.add_parser("invite", help="Make a new sign-up code: its digest for the config, the code for you")
    reset = commands.add_parser("reset-password", help="Set a new password for an account; it is read from stdin")
    reset.add_argument("--config", type=Path, required=True)
    reset.add_argument("email")
    people = commands.add_parser("accounts", help="List accounts: email, slot, and how they sign in")
    people.add_argument("--config", type=Path, required=True)
    ready = commands.add_parser("prepare", help="Seed each slot's data folder")
    ready.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "links":
        digests, urls = new_links([f"judge-{i + 1}" for i in range(args.count)], args.origin)
        code, invite = new_invite()
        print(f'invite_digest = "{invite}"')
        print("[slots]")
        for name, value in digests.items():
            print(f'{name} = "{value}"')
        print(
            "\n# The private links and sign-up code. Keep these out of the server and the repository.", file=sys.stderr
        )
        print(f"# invite code: {code}", file=sys.stderr)
        for name, url in urls.items():
            print(f"# {name}: {url}", file=sys.stderr)
        return 0
    if args.command == "invite":
        code, value = new_invite()
        print(f'invite_digest = "{value}"')
        print(f"# invite code: {code}", file=sys.stderr)
        return 0
    config = load(args.config)
    if args.command in ("reset-password", "accounts"):
        if config.accounts_db is None:
            raise ConfigError("this deployment has no accounts")
        store = Accounts(config.accounts_db, config.members)
        if args.command == "accounts":
            for account in store.listed():
                ways = [w for w, on in (("password", account.has_password), ("google", account.google)) if on]
                print(account.email, account.slot, "+".join(ways))
            return 0
        done = store.set_password(args.email, hash_password(sys.stdin.readline().rstrip("\n")))
        print("password changed; the account's sessions have ended" if done else "no such account")
        return 0 if done else 1
    if args.command == "prepare":
        prepare(config)
        return 0
    if not (config.bundle / "index.html").is_file():
        raise ConfigError(f"no built app at {config.bundle}")
    uvicorn.run(create_app(config), host=args.host, port=args.port, log_level="warning", proxy_headers=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
