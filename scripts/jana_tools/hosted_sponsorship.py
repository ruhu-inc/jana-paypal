"""Optional sponsored workshop for the existing hosted Study application and a separate sponsor origin."""

from __future__ import annotations

import asyncio
import hashlib
import html
import json
import logging
import re
import sqlite3
import subprocess
import sys
import time
import tomllib
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
from agent_core.config.trust import TrustStore
from agent_core.engine.events import new_id
from agent_core.store.study_sponsorship import eligible
from cloud_gateway.catalogue import export
from cloud_gateway.config import GatewayConfig, load_deployment
from cloud_gateway.providers.vertex import vertex_scope
from cloud_gateway.tier import monthly_allowance_tokens
from signing_service.hierarchy import CommandSigner, Signer
from starlette.applications import Starlette
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.routing import Host, Route, Router

from jana_tools.hosted_accounts import Attempts, verify_password
from jana_tools.sponsored_places import PRICE, FundingError, Places, Sandbox

log = logging.getLogger("jana.hosted.sponsorship")


@dataclass(frozen=True)
class Member:
    account_id: int
    slot: str
    profile: str
    licence: str
    source: str
    revision: int


def workshop_gateway(path: Path) -> GatewayConfig:
    """The PayPal workshop offers one text model, served by Google Vertex AI."""
    gateway = load_deployment(path)
    slot, alias = gateway.study_model.split("/")
    name = gateway.slots.get(slot)
    provider = gateway.providers.get(name) if name else None
    if (
        slot != "primary"
        or set(gateway.slots) != {"primary"}
        or set(gateway.providers) != {name}
        or provider is None
        or provider.adapter != "vertex"
        or alias not in provider.models
        or any(model.id != "gemini-3.8-flash" for model in provider.models.values())
    ):
        raise FundingError("Configure only Vertex Gemini 3.8 Flash for the sponsored Study workshop.")
    vertex_scope(provider.base_url)
    return gateway


class Workshop:
    def __init__(self, config, accounts, cores, client, *, gateway_loader=workshop_gateway):
        settings = tomllib.loads(config.sponsored_file.read_text())
        self.webhook_id = settings.get("webhook_id")
        self.config, self.accounts, self.cores, self.client = config, accounts, cores, client
        roster = {(a.account_id, a.slot) for a in accounts.listed()} if accounts else set()
        self.owner, self.wid = settings["sponsor_account"], settings["workshop_id"]
        self.origin = settings["sponsor_origin"]
        members = [Member(**item) for item in settings["members"]]
        self.members = {m.slot: m for m in members}
        if (
            not accounts
            or len(self.members) != 3
            or len({m.account_id for m in members}) != 3
            or self.owner in {m.account_id for m in members}
            or self.owner not in {account_id for account_id, _ in roster}
            or any((m.account_id, m.slot) not in roster for m in members)
            or set(self.members) - set(config.members)
            or not re.fullmatch(r"https://[a-z0-9.-]+", self.origin)
            or not config.secure_cookie
            or urlsplit(self.origin).hostname == urlsplit(config.public_origin).hostname
        ):
            raise FundingError("Configure a separate sponsor host and three enrolled personal learner accounts.")
        for m in members:
            if (
                not re.fullmatch(r"prf_[0-9A-HJKMNP-TV-Z]{26}", m.profile)
                or not re.fullmatch(r"src_[0-9A-HJKMNP-TV-Z]{26}", m.source)
                or type(m.revision) is not int
                or m.revision < 1
                or not re.fullmatch(r"lic_[A-Za-z0-9_-]{1,60}", m.licence)
            ):
                raise FundingError("Invalid profile, worksheet revision or licence enrollment.")
        gateway = gateway_loader(Path(config.gateway_config))
        anchors = Path(settings["anchors"])
        cert = TrustStore.from_document(json.loads(anchors.read_text())).certificates["licence.prepaid-sandbox"]
        if (
            gateway.environment != "dev"
            or gateway.open_registration
            or gateway.entitlement_keys != {cert.key_id: cert.public_key}
            or (gateway.licences and any(m.licence not in gateway.licences for m in members))
        ):
            raise FundingError("Use an isolated development gateway with only the enrolled Sandbox signer.")
        self.signer = Signer(
            cert, CommandSigner((sys.executable, "-m", "jana_tools.prepaid_signer", "sign", str(anchors.parent)))
        )
        self.pay = Places(
            Path(settings["database"]),
            Sandbox(Path(settings["credentials_file"])),
            gateway,
            merchant=settings["merchant"],
            payer=settings["payer"],
            origin=self.origin,
        )
        if monthly_allowance_tokens(gateway) <= 0:
            raise FundingError("A hosted sponsored month needs a positive tutoring allowance.")
        self.pay.register(
            self.wid, str(self.owner), [(m.profile, m.licence) for m in members], settings.get("window", 1800)
        )
        # The same funding attempt must never be rebound to another account, source or core after a restart.
        binding = hashlib.sha256(
            json.dumps(sorted([asdict(m) for m in members], key=lambda m: m["slot"]), sort_keys=True).encode()
        ).hexdigest()
        with self.pay.write() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS notifications(
                event_id TEXT PRIMARY KEY, digest TEXT NOT NULL, workshop TEXT NOT NULL, observed INTEGER NOT NULL)""")
            conn.execute("CREATE TABLE IF NOT EXISTS capture_audit(workshop TEXT PRIMARY KEY,checked REAL NOT NULL)")
            conn.execute("CREATE TABLE IF NOT EXISTS roster(workshop TEXT PRIMARY KEY,digest TEXT NOT NULL)")
            conn.execute("INSERT OR IGNORE INTO roster VALUES(?,?)", (self.wid, binding))
            if conn.execute("SELECT digest FROM roster WHERE workshop=?", (self.wid,)).fetchone()[0] != binding:
                raise FundingError("This workshop belongs to a different roster.")
        self.lock, self.logins = asyncio.Lock(), Attempts(10, 300)
        for m in members:
            cores[m.slot].sponsored_anchors = anchors

    def service_ready(self):
        config = self.pay.config
        now = time.time()
        limit = config.quotas.deployment_budget_expires_at
        if (
            not limit
            or limit.timestamp() <= now
            or monthly_allowance_tokens(config) <= 0
            or "study" not in export(config, now)["services"]
        ):
            return False
        with httpx.Client(timeout=4, trust_env=False) as probe:
            response = probe.get(f"http://127.0.0.1:{self.config.gateway_port}/v1/ping", params={"upstream": "1"})
            return response.status_code == 204

    def qualifies(self, m):
        return eligible(self.cores[m.slot].data_dir, m.profile, m.source, m.revision, int(time.time() * 1000))

    def advance(self):
        row = self.pay.row(self.wid)
        if row["state"] == "creating":
            row = self.pay.fund(self.wid)
        if row["state"] in ("approval", "authorizing"):
            row = self.pay.authorize(self.wid)
        if row["state"] == "open" and row["deadline"] > self.pay.clock():
            return
        if row["state"] in ("open", "capturing", "voiding"):

            def check(profile):
                m = next(m for m in self.members.values() if m.profile == profile)
                if not self.service_ready():
                    raise FundingError("Study is temporarily unavailable.")
                return self.qualifies(m)

            row = self.pay.settle(self.wid, check)
        if row["state"] == "captured":
            # Reconcile recent money changes on a verified notification or a periodic merchant read.
            with self.pay.write() as conn:
                unseen = conn.execute(
                    "SELECT event_id FROM notifications WHERE workshop=? AND observed=0", (self.wid,)
                ).fetchall()
                audited = conn.execute("SELECT checked FROM capture_audit WHERE workshop=?", (self.wid,)).fetchone()
            paid = datetime.fromisoformat(row["paid_at"]).timestamp()
            if unseen or (time.time() < paid + 40 * 86400 and (not audited or time.time() - audited[0] >= 900)):
                capture = self.pay.paypal.call("GET", "/v2/payments/captures/" + row["capture_id"])
                if capture.get("id") != row["capture_id"]:
                    raise FundingError("Capture reconciliation needs merchant review.")
                if capture.get("status") != "COMPLETED":
                    self.pay.change(self.wid, "captured", "payment_review")
                with self.pay.write() as conn:
                    conn.execute("INSERT OR REPLACE INTO capture_audit VALUES(?,?)", (self.wid, time.time()))
                    conn.executemany("UPDATE notifications SET observed=1 WHERE event_id=?", [(r[0],) for r in unseen])
                if self.pay.row(self.wid)["state"] != "captured":
                    return
            for m in self.members.values():
                member = self.pay.member(self.wid, m.profile)
                if member["state"] == "funded" and not member["grant"]:
                    self.pay.grant(self.wid, m.profile, self.signer)

    async def poll(self):
        while True:
            try:
                async with self.lock:
                    await asyncio.to_thread(self.advance)
            except (ValueError, KeyError, OSError, httpx.HTTPError, sqlite3.Error, subprocess.SubprocessError) as error:
                log.warning("Sponsored workshop reconciliation pending (%s)", type(error).__name__)
            await asyncio.sleep(10)

    async def native(self, m, token, path, body=None):
        core = self.cores[m.slot]
        if not core.running() or not token:
            raise FundingError("Reopen this profile in Study.")
        headers = {"authorization": "Bearer " + core.token, "x-profile-token": token}
        if body is not None:
            headers["idempotency-key"] = new_id("cli")
        result = await self.client.request(
            "POST" if body is not None else "GET",
            f"http://127.0.0.1:{core.port}/v1/" + path,
            headers=headers,
            json=body,
        )
        result.raise_for_status()
        return result.json()

    async def learner(self, request):
        from jana_tools.hosted import COOKIE, SESSION_COOKIE

        account = self.accounts.from_session(request.cookies.get(SESSION_COOKIE))
        member = self.members.get(account.slot) if account else None
        if not member or account.account_id != member.account_id or request.cookies.get(COOKIE):
            return Response(status_code=404)
        if request.method == "POST" and request.headers.get("origin") != self.config.public_origin:
            return Response(status_code=403)
        try:
            token = request.headers.get("x-profile-token")
            personal = await self.native(member, token, "profiles/" + member.profile)
            if personal["age_band"] != "18_plus" or personal["profile_id"] != member.profile:
                return Response(status_code=404)
            if request.path_params["source"] != member.source or request.path_params["revision"] != member.revision:
                return Response(status_code=404)
            async with self.lock:
                row = self.pay.row(self.wid)
                if request.method == "POST":
                    if len(await request.body()) > 1024:
                        return Response(status_code=413)
                    data = await request.json()
                    key = request.headers.get("idempotency-key", "")
                    if (
                        not re.fullmatch(r"cli_[A-Za-z0-9_-]{1,60}", key)
                        or not isinstance(data, dict)
                        or set(data) != {"action"}
                    ):
                        return Response(status_code=400)
                    action = data["action"]
                    if action == "claim":
                        if not await asyncio.to_thread(self.qualifies, member) or not await asyncio.to_thread(
                            self.service_ready
                        ):
                            raise FundingError("This activity is not eligible for a new place.")
                        self.pay.claim(self.wid, member.profile, key=key)
                    elif action == "withdraw":
                        self.pay.claim(self.wid, member.profile, withdraw=True, key=key)
                    elif action == "activate":
                        await self.activate(member, token)
                    else:
                        return Response(status_code=400)
                state = self.pay.member(self.wid, member.profile)["state"]
                if row["state"] == "payment_review":
                    state = "payment_review"
                if state == "claimed" and row["deadline"] <= self.pay.clock():
                    state = "committed"
                if state == "applied":
                    grant = json.loads(self.pay.member(self.wid, member.profile)["grant"])
                    if datetime.fromisoformat(grant["period_end"]).timestamp() <= time.time():
                        state = "ended"
                if state in ("invited", "withdrawn"):
                    available = (
                        row["state"] == "open"
                        and time.time() < row["deadline"]
                        and await asyncio.to_thread(self.qualifies, member)
                        and await asyncio.to_thread(self.service_ready)
                    )
                    state = "available" if available else "unavailable"
                return JSONResponse(
                    {"state": state, "deadline": row["deadline"]}, headers={"cache-control": "no-store"}
                )
        except (FundingError, httpx.HTTPError, sqlite3.Error, ValueError):
            return Response(status_code=409)

    async def activate(self, m, token):
        row = self.pay.row(self.wid)
        if row["state"] != "captured":
            raise FundingError("Waiting for funds.")
        if self.pay.member(self.wid, m.profile)["state"] == "applied":
            return
        current = await self.native(m, token, "subscription")
        # Detect refunds/reversals before activating an owed seat.
        capture = await asyncio.to_thread(self.pay.paypal.call, "GET", "/v2/payments/captures/" + row["capture_id"])
        if capture.get("id") != row["capture_id"] or capture.get("status") != "COMPLETED":
            raise FundingError("The payment needs reconciliation.")
        signed = await asyncio.to_thread(self.pay.grant, self.wid, m.profile, self.signer)
        if datetime.fromisoformat(signed["period_end"]).timestamp() <= time.time():
            raise FundingError("The funded month has ended.")
        if current["subscription_id"] != signed["subscription_id"]:
            if current["state"] in ("active", "pending_consent"):
                raise FundingError("This profile already has coverage.")
            current = await self.native(m, token, "subscription", {"entitlement": signed, "consents": []})
        if current["state"] != "active" or current["subscription_id"] != signed["subscription_id"]:
            raise FundingError("Activation is pending.")
        with self.pay.write() as conn:
            conn.execute(
                "UPDATE members SET state='applied' WHERE workshop=? AND profile=? AND state='funded'",
                (self.wid, m.profile),
            )

    async def sponsor(self, request):
        from jana_tools.hosted import _page as page

        session = "jana_workshop_sponsor"
        account = self.accounts.from_session(request.cookies.get(session))
        if request.method == "POST" and request.headers.get("origin") != self.origin:
            return Response(status_code=403)
        if not account or account.account_id != self.owner:
            if request.method == "POST" and request.url.path == "/signin":
                raw = await request.body()
                if len(raw) > 8192 or not self.logins.allow("coordinator"):
                    return Response(status_code=429)
                form = parse_qs(raw.decode())
                email = form.get("email", [""])[0].strip().lower()
                person = self.accounts.by_email(email)
                if (
                    not person
                    or person.account_id != self.owner
                    or not await asyncio.to_thread(
                        verify_password, form.get("password", [""])[0], self.accounts.password_hash(email)
                    )
                ):
                    return Response(status_code=403)
                response = RedirectResponse("/", 303)
                response.set_cookie(
                    session, self.accounts.start_session(self.owner), httponly=True, secure=True, samesite="lax"
                )
                return response
            return page(
                '<h1>Workshop coordinator</h1><form method="post" action="/signin">'
                '<label>Email <input name="email" type="email" required></label>'
                '<label>Password <input name="password" type="password" required></label>'
                "<button>Sign in</button></form>"
            )
        if request.method == "POST" and request.url.path == "/signout":
            self.accounts.end_session(request.cookies.get(session))
            response = RedirectResponse("/", 303)
            response.delete_cookie(session)
            return response
        row = self.pay.row(self.wid)
        if request.method == "POST" and request.url.path == "/sponsor/fund":
            if await request.body() != b"offer=" + row["binding"].encode():
                return Response(status_code=409)
            try:
                async with self.lock:
                    if row["state"] == "draft" and not await asyncio.to_thread(self.service_ready):
                        return page("<p>Online Study is temporarily unavailable.</p>", 503)
                    row = await asyncio.to_thread(self.pay.fund, self.wid)
                if row["state"] == "approval":
                    return RedirectResponse(row["approval"], 303)
            except (FundingError, httpx.HTTPError):
                return page("<p>PayPal confirmation is pending. Return here to check this workshop.</p>", 503)
        content = "<h1>Sponsored Study places</h1><p>US workshop · 3 adult places · $12.00 per month-long place.</p>"
        if row["state"] in ("draft", "creating", "approval"):
            content += (
                f"<p>Approve a $36.00 hold. Claims stay open for {row['window']} seconds after authorization. "
                "We charge $12.00 per eligible claim, up to $36.00. Zero claims means no charge. No renewal.</p>"
                f'<form method="post" action="/sponsor/fund"><button name="offer" value="{row["binding"]}">'
                "Approve maximum in PayPal Sandbox</button></form>"
            )
        elif row["state"] == "open":
            until = datetime.fromtimestamp(row["deadline"], UTC).strftime("%H:%M:%S UTC on %d %b")
            content += f"<p>$36.00 authorized. Claims close at {until}.</p>"
        elif row["state"] == "captured":
            content += f"<p>{row['frozen']} places funded · USD {PRICE * row['frozen'] / 100:.2f} charged.</p>"
            with self.pay.write() as conn:
                applied = conn.execute(
                    "SELECT count(*) FROM members WHERE workshop=? AND state='applied'", (self.wid,)
                ).fetchone()[0]
            content += f"<p>{applied} activated.</p>"
            content += f"<p>Sandbox receipt {html.escape(row['capture_id'])}</p>"
        elif row["state"] == "payment_review":
            content += "<p>The payment changed after settlement. Contact the workshop operator with your receipt.</p>"
        elif row["state"] in ("voided", "expired"):
            content += "<p>0 places funded · $0.00 charged. Your bank may take time to release the hold.</p>"
        else:
            content += "<p>Payment being confirmed.</p>"
        content += '<form method="post" action="/signout"><button>Sign out</button></form>'
        return page(content + '<p><a href="/">Refresh workshop status</a></p>')

    async def webhook(self, request):
        if not self.webhook_id:
            return Response(status_code=404)
        fields = {"webhook_id": self.webhook_id}
        for key in ("auth-algo", "cert-url", "transmission-id", "transmission-sig", "transmission-time"):
            values = request.headers.getlist("paypal-" + key)
            if len(values) != 1 or not 0 < len(values[0]) <= 2048:
                return Response(status_code=400)
            fields[key.replace("-", "_")] = values[0]
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > 131072:
                return Response(status_code=413)
            body.extend(chunk)
        try:
            event = json.loads(body)
            eid = event["id"]
            if not isinstance(eid, str) or not re.fullmatch(r"[A-Za-z0-9-]{1,128}", eid):
                return Response(status_code=400)
            envelope = json.dumps(fields).encode()[:-1] + b',"webhook_event":' + body + b"}"
            result = await asyncio.to_thread(
                self.pay.paypal.call, "POST", "/v1/notifications/verify-webhook-signature", raw=envelope
            )
            if result.get("verification_status") != "SUCCESS":
                return Response(status_code=403)
            res = event["resource"]
            parents = res.get("supplementary_data", {}).get("related_ids", {})
            remote_ids = {res.get("id"), *(parents.get(key) for key in ("order_id", "authorization_id", "capture_id"))}
            row = self.pay.row(self.wid)
            if not remote_ids.intersection({row["order_id"], row["auth_id"], row["capture_id"]} - {None}):
                return Response(status_code=204)
            digest = hashlib.sha256(body).hexdigest()
            with self.pay.write() as conn:
                conn.execute("INSERT OR IGNORE INTO notifications VALUES(?,?,?,0)", (eid, digest, self.wid))
                if conn.execute("SELECT digest FROM notifications WHERE event_id=?", (eid,)).fetchone()[0] != digest:
                    return Response(status_code=409)
            return Response(status_code=204)
        except (ValueError, TypeError, AttributeError, KeyError):
            return Response(status_code=400)
        except (httpx.HTTPError, sqlite3.Error):
            return Response(status_code=503)

    def sponsor_host(self):
        sponsor = Starlette(
            routes=[
                Route("/hooks/paypal/sandbox", self.webhook, methods=["POST"]),
                *[
                    Route(path, self.sponsor, methods=methods)
                    for path, methods in [
                        ("/", ["GET"]),
                        ("/signin", ["POST"]),
                        ("/signout", ["POST"]),
                        ("/sponsor/fund", ["POST"]),
                        ("/sponsor/return", ["GET"]),
                        ("/sponsor/cancel", ["GET"]),
                    ]
                ],
            ]
        )
        return Host(urlsplit(self.origin).hostname, sponsor)

    def routes(self, existing):
        return [
            self.sponsor_host(),
            Host(
                urlsplit(self.config.public_origin).hostname,
                Router(
                    routes=[
                        Route("/core/study-place/{source}/{revision:int}", self.learner, methods=["GET", "POST"]),
                        *existing,
                    ]
                ),
            ),
        ]
