"""Private PayPal Sandbox rehearsal. Both entry points bind only to loopback."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import hmac
import html
import logging
import secrets
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import uvicorn
from agent_core.engine.events import new_id
from cloud_gateway.config import ServicePermission
from starlette.applications import Starlette
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.routing import Host, Route, Router

from jana_tools.hosted import Core, _page, load
from jana_tools.hosted_accounts import Accounts, Attempts, verify_password
from jana_tools.hosted_sponsorship import Workshop, workshop_gateway
from jana_tools.sponsored_places import FundingError

COOKIE = "__Host-jana-adult-workshop"
QUESTION = "What is the cyclist's average speed? Show the calculation using the worksheet."
WORKSHEET = (
    "A cyclist covers 120 metres in 20 seconds on a level cycle path. Average speed equals total distance divided "
    "by elapsed time. The average speed on this trip is 6 metres per second. Speed has magnitude only. "
    "The cyclist can change direction without changing speed."
)
log = logging.getLogger("jana.workshop")


class WorkshopPermission(ServicePermission):
    def permits(self, now: float) -> bool:
        return self.audiences == {"adult"} and now < self.expires_at.timestamp()


def adult_gateway(path):
    config = workshop_gateway(path)
    quotas = config.quotas
    name = config.slots["primary"]
    provider = config.providers[name]
    if (
        config.environment != "dev"
        or config.audience_host != "localhost"
        or config.open_registration
        or len(set(config.licences)) != 3
        or len(config.entitlement_keys) != 1
        or not quotas.deployment_budget_expires_at
        or not quotas.deployment_daily_spend_usd
        or not quotas.deployment_total_spend_usd
        or not provider.permission
        or provider.permission.audiences != {"adult"}
    ):
        raise FundingError("Configure the closed adult staging gateway, its provider terms and spending limits.")
    scoped = WorkshopPermission(**provider.permission.model_dump())
    return config.model_copy(update={"providers": {name: provider.model_copy(update={"permission": scoped})}})


def gateway_app(path):
    from cloud_gateway.app import create_app

    app = create_app(adult_gateway(path))
    allowed = {
        "/v1/ping",
        "/v1/devices",
        "/v1/auth/challenge",
        "/v1/auth/token",
        "/v1/study/process",
        "/v1/study/uploads",
        "/v1/tutoring/usage",
    }
    app.router.routes[:] = [
        r
        for r in app.router.routes
        if getattr(r, "path", "") in allowed or getattr(r, "path", "") == "/v1/study/requests/{request_id}"
    ]
    return app


def button(action, label):
    return f'<form method="post" action="/{action}"><button>{html.escape(label)}</button></form>'


class LearnerWorkshop:
    def __init__(self, workshop):
        self.workshop = workshop
        self.key = secrets.token_bytes(32)
        self.attempts = Attempts(15, 300)
        self.locks = {slot: asyncio.Lock() for slot in workshop.members}
        self.profiles = {}
        with workshop.pay.write() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS adult_sessions(
                workshop TEXT, profile TEXT, command TEXT NOT NULL, job TEXT, finished INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(workshop,profile))""")

    def sign(self, raw):
        return hmac.new(self.key, raw.encode(), hashlib.sha256).hexdigest()

    def member(self, request):
        raw, _, signature = request.cookies.get(COOKIE, "").partition(".")
        if not raw or not hmac.compare_digest(self.sign(raw).encode(), signature.encode()):
            return None
        account = self.workshop.accounts.from_session(raw)
        member = self.workshop.members.get(account.slot) if account else None
        return member if member and member.account_id == account.account_id else None

    async def signin(self, request):
        if request.method == "GET":
            return _page(
                "<h1>Adult Study workshop</h1><p>Use the private learner account from your invitation.</p>"
                '<form action="/signin" method="post"><label>Email <input type="email" name="email" required>'
                '</label><label>Password <input type="password" name="password" required></label>'
                '<label><input style="width:auto" type="checkbox" name="adult" value="yes" required> '
                "I am 18 or older.</label><button>Open my worksheet</button></form>"
            )
        if request.headers.get("origin") != self.workshop.config.public_origin:
            return Response(status_code=403)
        raw = bytearray()
        async for chunk in request.stream():
            if len(raw) + len(chunk) > 4096:
                return Response(status_code=413)
            raw.extend(chunk)
        data = parse_qs(raw.decode(errors="replace"))
        if data.get("adult") != ["yes"]:
            return _page("<p>This private workshop requires an adult learner.</p>", 403)
        if not self.attempts.allow(request.client.host if request.client else "local"):
            return Response(status_code=429)
        addr = data.get("email", [""])[0]
        accounts = self.workshop.accounts
        valid = await asyncio.to_thread(verify_password, data.get("password", [""])[0], accounts.password_hash(addr))
        account = accounts.by_email(addr) if valid else None
        member = self.workshop.members.get(account.slot) if account else None
        if not member or member.account_id != account.account_id:
            return _page("<p>Check the private learner account details in your invitation.</p>", 403)
        token = accounts.start_session(account.account_id)
        response = RedirectResponse("/", 303)
        response.set_cookie(COOKIE, token + "." + self.sign(token), secure=True, httponly=True, samesite="lax")
        return response

    async def call(self, member, method, path, body=None, *, token=None, opened=None, key=None):
        core = self.workshop.cores[member.slot]
        headers = {"authorization": "Bearer " + core.token}
        if method != "GET":
            headers["idempotency-key"] = key or new_id("cli")
        if token:
            headers["x-profile-token"] = token
        if opened:
            headers["x-study-open"] = opened
        reply = await self.workshop.client.request(
            method, f"http://127.0.0.1:{core.port}/v1/{path}", headers=headers, json=body
        )
        reply.raise_for_status()
        return reply.json() if reply.content else None

    async def profile(self, m):
        core = self.workshop.cores[m.slot]
        await core.ensure()
        token = self.profiles.get(m.slot)
        if token:
            try:
                await self.call(m, "GET", "profiles/" + m.profile, token=token)
                return token
            except httpx.HTTPStatusError as error:
                if error.response.status_code not in (401, 403, 404, 409):
                    raise
        record = await self.call(m, "POST", f"profiles/{m.profile}/open", {})
        token = record["profile_token"]
        profile = await self.call(m, "GET", "profiles/" + m.profile, token=token)
        if profile["profile_id"] != m.profile or profile["age_band"] != "18_plus":
            raise FundingError("Only enrolled adult profiles can open this workshop.")
        self.profiles[m.slot] = token
        return token

    async def worksheet(self, m, token):
        source = "study/sources/" + m.source
        record = await self.call(m, "POST", source + "/open", {"confirm": True, "revision": m.revision}, token=token)
        opened = record["open_token"]
        content = await self.call(m, "GET", f"{source}/content?revision={m.revision}", token=token, opened=opened)
        text = " ".join(chunk["text"] for chunk in content["items"])
        if record["source"]["state"] != "ready" or content["next_cursor"] is not None or text != WORKSHEET:
            raise FundingError("The enrolled public worksheet has changed.")
        return opened

    def progress(self, m):
        with self.workshop.pay.write() as db:
            return db.execute(
                "SELECT * FROM adult_sessions WHERE workshop=? AND profile=?", (self.workshop.wid, m.profile)
            ).fetchone()

    async def study(self, m, token, opened, action):
        work = self.workshop
        row = self.progress(m)
        if action == "study" and not row:
            with work.pay.write() as db:
                db.execute(
                    "INSERT OR IGNORE INTO adult_sessions(workshop,profile,command) VALUES (?,?,?)",
                    (work.wid, m.profile, new_id("cli")),
                )
            row = self.progress(m)
        if not row:
            return (
                f"<p>{html.escape(QUESTION)}</p><p>Online Study sends this question and worksheet excerpts to "
                "Google Vertex AI using Gemini 3.8 Flash. Jana saves the response in your Study profile.</p>"
                + button("study", "Share this worksheet and start Study")
            )
        if not row["job"]:
            if action != "study":
                return button("study", "Resume my Study request")
            for scope in ("cloud_tier", "study_materials"):
                await self.call(m, "PUT", f"profiles/{m.profile}/consents/{scope}", {"action": "on"}, token=token)
            await self.call(
                m, "PATCH", "connectivity", {"features": {"study_materials": True, "cloud_tier": True}}, token=token
            )
            await self.call(m, "POST", "study/session", {}, token=token)
            job = await self.call(
                m,
                "POST",
                "study/jobs",
                {
                    "kind": "chat",
                    "execution": "prefer_cloud",
                    "sources": [{"source_id": m.source, "revision": m.revision}],
                    "prompt": QUESTION,
                },
                token=token,
                opened=opened,
                key=row["command"],
            )
            with work.pay.write() as db:
                db.execute(
                    "UPDATE adult_sessions SET job=? WHERE workshop=? AND profile=?",
                    (job["job_id"], work.wid, m.profile),
                )
            row = self.progress(m)
        job = await self.call(m, "GET", "study/jobs/" + row["job"], token=token, opened=opened)
        if job["state"] in ("failed", "paused", "waiting_online"):
            if action == "retry":
                await self.call(
                    m,
                    "POST",
                    "study/jobs/" + row["job"] + "/actions",
                    {"action": "retry" if job["state"] == "failed" else "resume"},
                    token=token,
                    opened=opened,
                )
            return (
                "<p>Study is waiting. "
                + html.escape(job["error"] or job["state"])
                + ".</p>"
                + button("retry", "Try again")
            )
        if job["state"] != "completed":
            return "<p>Your worksheet question is processing. Refresh to see the answer.</p>"
        aid = await self.call(m, "GET", "study/aids/" + job["result_ids"][0], token=token, opened=opened)
        if aid["status"] != "ready" or (aid.get("made_by") or {}).get("runtime") != "online":
            raise FundingError("An online Study answer has not been verified.")
        content = "<h2>Answer from Gemini</h2>"
        for claim in aid["claims"]:
            content += "<p>" + html.escape(claim["text"]) + "</p>"
            for cite in claim["citations"]:
                if cite["source_id"] != m.source or cite["revision"] != m.revision or cite["quote"] not in WORKSHEET:
                    raise FundingError("A source citation could not be verified.")
                content += "<blockquote>" + html.escape(cite["quote"]) + "</blockquote>"
        if action == "finish" and not row["finished"]:
            await self.call(m, "DELETE", "study/session", token=token)
            with work.pay.write() as db:
                db.execute("UPDATE adult_sessions SET finished=1 WHERE workshop=? AND profile=?", (work.wid, m.profile))
        return content + (
            "<p>Study session complete. Your answer is saved.</p>"
            if self.progress(m)["finished"]
            else button("finish", "Finish Study session")
        )

    async def learner(self, request):
        w = self.workshop
        if request.method == "POST" and request.headers.get("origin") != w.config.public_origin:
            return Response(status_code=403)
        m = self.member(request)
        if not m:
            return RedirectResponse("/signin", 303)
        action = request.url.path.strip("/")
        if action == "signout":
            w.accounts.end_session(request.cookies[COOKIE].split(".")[0])
            response = RedirectResponse("/signin", 303)
            response.delete_cookie(COOKIE, secure=True, httponly=True)
            return response
        try:
            async with self.locks[m.slot]:
                token = await self.profile(m)
                opened = await self.worksheet(m, token)
                async with w.lock:
                    if action == "claim":
                        if not await asyncio.to_thread(w.qualifies, m) or not await asyncio.to_thread(w.service_ready):
                            raise FundingError("This activity currently has no available sponsored place.")
                        w.pay.claim(w.wid, m.profile)
                    elif action == "withdraw":
                        w.pay.claim(w.wid, m.profile, withdraw=True)
                    elif action == "activate":
                        await w.activate(m, token)
                    offer = w.pay.row(w.wid)
                    state = w.pay.member(w.wid, m.profile)["state"]
                if action in ("study", "retry", "finish") and (offer["state"], state) != ("captured", "applied"):
                    raise FundingError("Activate the funded month before starting online Study.")
                body = "<h1>Average speed on a cycle path</h1><p>" + html.escape(WORKSHEET) + "</p>"
                if offer["state"] == "captured" and state == "applied":
                    body += "<p>Your sponsored month is active.</p>" + await self.study(m, token, opened, action)
                elif offer["state"] == "captured" and state == "funded":
                    body += button("activate", "Activate my sponsored month")
                elif offer["state"] == "open" and offer["deadline"] > w.pay.clock():
                    body += (
                        "<p>Your place is claimed.</p>" + button("withdraw", "Withdraw my claim")
                        if state == "claimed"
                        else button("claim", "Claim my sponsored month")
                    )
                elif state == "claimed":
                    body += "<p>Your place is claimed. Refresh after the sponsor payment settles.</p>"
                elif offer["state"] in ("draft", "creating", "approval", "authorizing"):
                    body += "<p>Waiting for sponsor approval. Refresh after the coordinator approves in PayPal.</p>"
                else:
                    body += "<p>The claim window has closed.</p>"
                return _page(body + '<p><a href="/">Refresh my workshop</a></p>' + button("signout", "Sign out"))
        except (FundingError, httpx.HTTPError, TimeoutError) as error:
            status = error.response.status_code if isinstance(error, httpx.HTTPStatusError) else None
            log.warning("Learner workshop: action=%s failure=%s status=%s", action, type(error).__name__, status)
            return _page('<p>This step could not be completed. <a href="/">Check your workshop</a>.</p>', 409)


def front_app(path):
    cfg = load(path)
    if not cfg.accounts_db or not cfg.sponsored_file or cfg.invite_digest or cfg.google_client_id or cfg.warm:
        raise FundingError("The adult workshop requires closed enrollment.")
    accounts = Accounts(cfg.accounts_db, cfg.members)
    cores = {slot: Core(slot, cfg, adult_workshop=True) for slot in cfg.members}
    client = httpx.AsyncClient(trust_env=False, timeout=180)
    w = Workshop(cfg, accounts, cores, client, gateway_loader=adult_gateway)
    if set(w.pay.config.licences) != {m.licence for m in w.members.values()}:
        raise FundingError("Match the three device licences to the enrolled adults.")
    learner = LearnerWorkshop(w)

    @contextlib.asynccontextmanager
    async def lifespan(app):
        polling = asyncio.create_task(w.poll())
        try:
            yield
        finally:
            polling.cancel()
            await asyncio.gather(polling, return_exceptions=True)
            await asyncio.gather(*(core.stop() for core in cores.values()), return_exceptions=True)
            await client.aclose()

    async def healthy(request):
        return JSONResponse({"ok": True, "service": "adult-study-workshop"})

    routes = [
        Route("/healthz", healthy),
        Route("/signin", learner.signin, methods=["GET", "POST"]),
        Route("/", learner.learner),
    ]
    for action in ("claim", "withdraw", "activate", "study", "retry", "finish", "signout"):
        routes.append(Route("/" + action, learner.learner, methods=["POST"]))
    return Starlette(
        routes=[w.sponsor_host(), Host(urlsplit(cfg.public_origin).hostname, Router(routes=routes))], lifespan=lifespan
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("role", choices=["gateway", "front"])
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args()
    application = (gateway_app if args.role == "gateway" else front_app)(args.config)
    uvicorn.run(
        application, host="127.0.0.1", port=args.port, access_log=False, server_header=False, proxy_headers=False
    )
