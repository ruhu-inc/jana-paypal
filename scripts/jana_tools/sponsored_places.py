"""PayPal Sandbox funding for one three-place adult Study workshop. All amounts are US cents."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
import time
import tomllib
import uuid
from collections.abc import Callable, Iterator, Sequence
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
from cloud_gateway.config import GatewayConfig
from cloud_gateway.plans import entitlement_terms, quote
from cloud_gateway.tier import add_months

SANDBOX = "https://api-m.sandbox.paypal.com"
SEATS = 3
PRICE = 1200
MAXIMUM = SEATS * PRICE


class FundingError(ValueError):
    """Public, payment-data-free explanation."""


def amount(cents: int) -> dict:
    return {"currency_code": "USD", "value": f"{cents // 100}.{cents % 100:02d}"}


def matches_amount(value, cents: int) -> bool:
    expected = amount(cents)
    return isinstance(value, dict) and all(value.get(k) == v for k, v in expected.items())


def resource(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Z0-9]{1,36}", value):
        raise FundingError("PayPal returned an unrecognized resource.")
    return value


def request_key(workshop: str, operation: str) -> str:
    return str(uuid.uuid5(uuid.UUID(workshop), operation))


class Sandbox:
    def __init__(self, credentials: Path, *, transport=None):
        with credentials.expanduser().open("rb") as stream:
            mode = os.fstat(stream.fileno()).st_mode
            if not stat.S_ISREG(mode) or mode & 0o077:
                raise FundingError("Use an owner-only Sandbox credential file.")
            creds = tomllib.load(stream)
        self.auth = (creds["client_id"], creds["client_secret"])
        self.transport = transport

    def call(self, method, path, body=None, key=None, *, raw=None):
        with httpx.Client(base_url=SANDBOX, transport=self.transport, timeout=15, trust_env=False) as client:
            token = client.post("/v1/oauth2/token", auth=self.auth, data={"grant_type": "client_credentials"})
            token.raise_for_status()
            headers = {
                "Authorization": "Bearer " + token.json()["access_token"],
                "Prefer": "return=representation",
                "Content-Type": "application/json",
            }
            if key:
                headers["PayPal-Request-Id"] = key
            response = client.request(method, path, headers=headers, json=body, content=raw)
            response.raise_for_status()
            return response.json() if response.content else {}


class Places:
    def __init__(
        self,
        db: Path,
        paypal: Sandbox,
        config: GatewayConfig,
        *,
        merchant: str,
        payer: str,
        origin: str,
        clock: Callable[[], float] = time.time,
    ):
        self.db, self.paypal, self.config, self.clock = db, paypal, config, clock
        self.merchant, self.payer, self.origin = resource(merchant), resource(payer), origin
        parsed = urlsplit(origin)
        if (
            (parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname == "localhost"))
            or parsed.username
            or parsed.password
            or not parsed.netloc
            or parsed.path
            or parsed.fragment
            or parsed.query
        ):
            raise FundingError("Set a fixed HTTPS sponsor origin or local Sandbox return address.")
        self.offer = quote(config, "US", "jana_online", "month", seats=1)
        if (self.offer.amount * 100, self.offer.currency, self.offer.months) != (PRICE, "USD", 1):
            raise FundingError("The current US price differs from the sponsored offer.")
        with closing(self.connect()) as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS workshops(
                    id TEXT PRIMARY KEY, owner TEXT NOT NULL, binding TEXT NOT NULL, state TEXT NOT NULL,
                    started REAL NOT NULL, window INTEGER NOT NULL, deadline REAL, order_id TEXT UNIQUE,
                    approval TEXT, auth_id TEXT UNIQUE, capture_id TEXT UNIQUE, frozen INTEGER, phase_time REAL,
                    paid_at TEXT, auth_expires REAL);
                CREATE TABLE IF NOT EXISTS members(
                    workshop TEXT NOT NULL REFERENCES workshops(id), profile TEXT NOT NULL, licence TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'invited', grant TEXT, PRIMARY KEY(workshop,profile));
                CREATE TABLE IF NOT EXISTS offers(
                    workshop TEXT NOT NULL, profile TEXT NOT NULL, terms TEXT NOT NULL,
                    PRIMARY KEY(workshop,profile));
                CREATE TABLE IF NOT EXISTS commands(
                    workshop TEXT NOT NULL, profile TEXT NOT NULL, command TEXT NOT NULL, action TEXT NOT NULL,
                    PRIMARY KEY(workshop,profile,command));
                CREATE UNIQUE INDEX IF NOT EXISTS one_reservation ON members(profile)
                    WHERE state IN ('claimed','committed','funded');
            """)

    def connect(self):
        conn = sqlite3.connect(self.db, isolation_level=None, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        with closing(self.connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            yield conn

    def register(self, wid: str, owner: str, members: Sequence[tuple[str, str]], window=1800):
        uuid.UUID(wid)
        if len(members) != SEATS or len({p for p, _ in members}) != SEATS or not 1 <= window <= 1800:
            raise FundingError("Enroll three distinct adult profiles and a valid claim window.")
        binding = hashlib.sha256(
            json.dumps(
                [self.merchant, self.payer, self.origin, owner, sorted(members), window, PRICE], separators=(",", ":")
            ).encode()
        ).hexdigest()
        with self.write() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO workshops(id,owner,binding,state,started,window) VALUES(?,?,?,'draft',?,?)",
                (wid, owner, binding, self.clock(), window),
            )
            if conn.execute("SELECT binding FROM workshops WHERE id=?", (wid,)).fetchone()[0] != binding:
                raise FundingError("The enrollment differs from this workshop's stored offer.")
            conn.executemany(
                "INSERT OR IGNORE INTO members(workshop,profile,licence) VALUES(?,?,?)",
                [(wid, p, licence) for p, licence in members],
            )

            for profile, licence in members:
                when = datetime.fromtimestamp(self.clock(), UTC).isoformat(timespec="milliseconds")
                terms = entitlement_terms(
                    self.config,
                    self.offer,
                    subscription_id="sub_" + request_key(wid, profile),
                    licence_id=licence,
                    period_start=when,
                    created_at=when,
                    profile_ids=[profile],
                )
                conn.execute("INSERT OR IGNORE INTO offers VALUES(?,?,?)", (wid, profile, json.dumps(terms)))

    def row(self, wid):
        with closing(self.connect()) as conn:
            row = conn.execute("SELECT * FROM workshops WHERE id=?", (wid,)).fetchone()
        if row is None:
            raise FundingError("Workshop not found.")
        return row

    def change(self, wid, previous, new, **fields):
        allowed = {"approval", "order_id", "auth_id", "capture_id", "frozen", "deadline", "phase_time", "auth_expires"}
        if fields.keys() - allowed:
            raise ValueError("Invalid workshop field")
        columns = "".join(f", {k}=?" for k in fields)
        with self.write() as conn:
            conn.execute(
                "UPDATE workshops SET state=?" + columns + " WHERE id=? AND state=?",  # noqa: S608
                (new, *fields.values(), wid, previous),
            )

    def fund(self, wid):
        self.change(wid, "draft", "creating", phase_time=self.clock())
        row = self.row(wid)
        if row["state"] != "creating":
            return row
        self.retry_window(row)
        body = {
            "intent": "AUTHORIZE",
            "purchase_units": [
                {
                    "reference_id": "workshop-funding",
                    "custom_id": wid,
                    "description": "Up to three one-month adult Study places",
                    "payee": {"merchant_id": self.merchant},
                    "amount": amount(MAXIMUM),
                }
            ],
            "payment_source": {
                "paypal": {
                    "experience_context": {
                        "shipping_preference": "NO_SHIPPING",
                        "return_url": self.origin + "/sponsor/return",
                        "cancel_url": self.origin + "/sponsor/cancel",
                    }
                }
            },
        }
        result = self.paypal.call("POST", "/v2/checkout/orders", body, request_key(wid, "create"))
        oid = resource(result.get("id"))
        links = [v["href"] for v in result.get("links", []) if v.get("rel") in ("approve", "payer-action")]
        if len(links) != 1:
            raise FundingError("No unique PayPal approval link.")
        uri = urlsplit(links[0])
        if (uri.scheme, uri.netloc, uri.path) != ("https", "www.sandbox.paypal.com", "/checkoutnow") or parse_qs(
            uri.query
        ).get("token") != [oid]:
            raise FundingError("Unrecognized Sandbox approval link.")
        self.change(wid, "creating", "approval", order_id=oid, approval=links[0])
        return self.row(wid)

    def retry_window(self, row):
        if self.clock() - row["phase_time"] >= 6 * 3600:
            raise FundingError("This unresolved operation needs merchant reconciliation.")

    def payments(self, row, order):
        units = order.get("purchase_units", [])
        if (
            order.get("id") != row["order_id"]
            or order.get("intent") != "AUTHORIZE"
            or len(units) != 1
            or order.get("payer", {}).get("payer_id") != self.payer
            or order.get("payer", {}).get("address", {}).get("country_code") != "US"
        ):
            raise FundingError("PayPal order or approving sponsor did not match.")
        unit = units[0]
        if (
            unit.get("custom_id") != row["id"]
            or unit.get("reference_id") != "workshop-funding"
            or unit.get("payee", {}).get("merchant_id") != self.merchant
            or not matches_amount(unit.get("amount"), MAXIMUM)
        ):
            raise FundingError("PayPal merchant, amount or workshop did not match.")
        return unit.get("payments", {})

    def authorize(self, wid):
        row = self.row(wid)
        if row["state"] not in ("approval", "authorizing"):
            return row
        path = "/v2/checkout/orders/" + resource(row["order_id"])
        order = self.paypal.call("GET", path)
        if order.get("status") in ("CREATED", "PAYER_ACTION_REQUIRED"):
            return row
        payments = self.payments(row, order)
        if not payments.get("authorizations") and order.get("status") == "APPROVED":
            self.change(wid, "approval", "authorizing", phase_time=self.clock())
            self.retry_window(self.row(wid))
            self.paypal.call("POST", path + "/authorize", {}, request_key(wid, "authorize"))
            payments = self.payments(row, self.paypal.call("GET", path))
        auths = payments.get("authorizations", [])
        if len(auths) != 1 or payments.get("captures"):
            raise FundingError("Expected one authorization and zero captures.")
        aid = resource(auths[0].get("id"))
        auth = self.paypal.call("GET", "/v2/payments/authorizations/" + aid)
        if auth.get("id") != aid or not matches_amount(auth.get("amount"), MAXIMUM) or auth.get("status") != "CREATED":
            raise FundingError("The $36 authorization is not available.")
        expires = datetime.fromisoformat(auth["expiration_time"]).timestamp()
        deadline = self.clock() + row["window"]
        if expires < deadline + 300:
            raise FundingError("The authorization expires before the workshop can settle.")
        self.change(wid, "approval", "authorizing", phase_time=self.clock())
        self.change(wid, "authorizing", "open", auth_id=aid, auth_expires=expires, deadline=deadline)
        return self.row(wid)

    def member(self, wid, profile):
        with closing(self.connect()) as conn:
            row = conn.execute("SELECT * FROM members WHERE workshop=? AND profile=?", (wid, profile)).fetchone()
        if row is None:
            raise FundingError("This profile is not enrolled.")
        return row

    def claim(self, wid, profile, *, withdraw=False, key=None):
        with self.write() as conn:
            state = "withdrawn" if withdraw else "claimed"
            if key is not None:
                existing = conn.execute(
                    "SELECT action FROM commands WHERE workshop=? AND profile=? AND command=?", (wid, profile, key)
                ).fetchone()
                if existing:
                    if existing[0] != state:
                        raise FundingError("This command already refers to a different action.")
                    return
                conn.execute("INSERT INTO commands VALUES(?,?,?,?)", (wid, profile, key, state))
            workshop = conn.execute("SELECT state,deadline FROM workshops WHERE id=?", (wid,)).fetchone()
            if not workshop or workshop["state"] != "open" or self.clock() >= workshop["deadline"]:
                raise FundingError("The claim window is closed.")
            try:
                changed = conn.execute(
                    "UPDATE members SET state=? WHERE workshop=? AND profile=?", (state, wid, profile)
                ).rowcount
            except sqlite3.IntegrityError:
                raise FundingError("A place is reserved through another workshop.") from None
            if not changed:
                raise FundingError("This profile is not enrolled.")

    def freeze(self, wid, eligible):
        # After the cutoff claim()/withdraw no longer admit writes. Network checks occur outside the write lock.
        row = self.row(wid)
        if row["state"] != "open":
            return
        if self.clock() < row["deadline"]:
            raise FundingError("Claims are still open.")
        with closing(self.connect()) as conn:
            claims = conn.execute(
                "SELECT profile FROM members WHERE workshop=? AND state='claimed' ORDER BY profile", (wid,)
            ).fetchall()
        # After a prolonged outage, void all unverified claims rather than leaving a buyer's hold open for weeks.
        verify_by = min(row["auth_expires"] - 300, row["deadline"] + 300)
        approved = [r[0] for r in claims if eligible(r[0])] if self.clock() < verify_by else []
        if self.clock() >= verify_by:
            approved = []
        with self.write() as conn:
            if conn.execute("SELECT state FROM workshops WHERE id=?", (wid,)).fetchone()[0] != "open":
                return
            conn.executemany(
                "UPDATE members SET state='committed' WHERE workshop=? AND profile=? AND state='claimed'",
                [(wid, p) for p in approved],
            )
            conn.execute("UPDATE members SET state='excluded' WHERE workshop=? AND state='claimed'", (wid,))
            conn.execute(
                "UPDATE workshops SET state=?,frozen=?,phase_time=? WHERE id=?",
                ("capturing" if approved else "voiding", len(approved), self.clock(), wid),
            )

    def remote(self, row):
        order = self.paypal.call("GET", "/v2/checkout/orders/" + resource(row["order_id"]))
        payments = self.payments(row, order)
        auths = payments.get("authorizations", [])
        if len(auths) != 1 or auths[0].get("id") != row["auth_id"]:
            raise FundingError("The authorization is not bound to the workshop order.")
        auth = self.paypal.call("GET", "/v2/payments/authorizations/" + resource(row["auth_id"]))
        if auth.get("id") != row["auth_id"] or not matches_amount(auth.get("amount"), MAXIMUM):
            raise FundingError("Authorization details no longer match.")
        return auth, payments.get("captures", [])

    def settle(self, wid, eligible):
        self.freeze(wid, eligible)
        row = self.row(wid)
        if row["state"] not in ("voiding", "capturing"):
            return row
        path = "/v2/payments/authorizations/" + resource(row["auth_id"])
        auth, captures = self.remote(row)
        if row["state"] == "voiding":
            if captures:
                raise FundingError("Unexpected capture on an empty workshop.")
            if auth["status"] == "CREATED":
                self.paypal.call("POST", path + "/void", key=request_key(wid, "void"))
                auth, captures = self.remote(row)
            if auth["status"] not in ("VOIDED", "EXPIRED") or captures:
                raise FundingError("Waiting for verification of the unused authorization.")
            self.change(wid, "voiding", "voided" if auth["status"] == "VOIDED" else "expired")
            return self.row(wid)

        total = amount(row["frozen"] * PRICE)
        cid = row["capture_id"]
        if not captures and not cid and auth["status"] == "CREATED":
            self.retry_window(row)
            receipt = self.paypal.call(
                "POST", path + "/capture", {"amount": total, "final_capture": True}, request_key(wid, "capture")
            )
            cid = resource(receipt.get("id"))
            self.change(wid, "capturing", "capturing", capture_id=cid)
            auth, captures = self.remote(row)
        if len(captures) > 1 or (cid and captures and cid != captures[0].get("id")):
            raise FundingError("Unexpected additional capture. Reconcile with the merchant.")
        cid = resource(cid or (captures[0].get("id") if captures else None))
        captured = self.paypal.call("GET", "/v2/payments/captures/" + cid)
        related = captured.get("supplementary_data", {}).get("related_ids", {})
        if (
            related.get("order_id", row["order_id"]) != row["order_id"]
            or related.get("authorization_id", row["auth_id"]) != row["auth_id"]
            or (
                not captures
                and related.get("authorization_id") != row["auth_id"]
                and related.get("order_id") != row["order_id"]
            )
            or captured.get("payee", {}).get("merchant_id", self.merchant) != self.merchant
            or captured.get("id") != cid
            or not matches_amount(captured.get("amount"), row["frozen"] * PRICE)
            or captured.get("final_capture") is not True
            or captured.get("status") != "COMPLETED"
            # Sandbox retains generic capture/void links on terminal authorizations.
            or auth.get("status") != "CAPTURED"
        ):
            raise FundingError("Waiting for a verified final capture for the exact claim total.")
        paid = datetime.fromisoformat(captured["update_time"]).astimezone(UTC).isoformat(timespec="milliseconds")
        with self.write() as conn:
            if conn.execute(
                "UPDATE workshops SET state='captured',capture_id=?,paid_at=? WHERE id=? AND state='capturing'",
                (cid, paid, wid),
            ).rowcount:
                conn.execute("UPDATE members SET state='funded' WHERE workshop=? AND state='committed'", (wid,))
        return self.row(wid)

    def grant(self, wid, profile, signer):
        # A failed signer leaves the paid seat owed. First successful issuance wins even with concurrent retries.
        row, member = self.row(wid), self.member(wid, profile)
        if row["state"] != "captured" or member["state"] not in ("funded", "applied"):
            raise FundingError("This place has no verified payment.")
        if not member["grant"]:
            with closing(self.connect()) as conn:
                terms = json.loads(
                    conn.execute("SELECT terms FROM offers WHERE workshop=? AND profile=?", (wid, profile)).fetchone()[
                        0
                    ]
                )
            start = datetime.fromisoformat(row["paid_at"]).timestamp()
            end = datetime.fromtimestamp(add_months(start, 1), UTC).isoformat(timespec="milliseconds")
            terms.update(period_start=row["paid_at"], created_at=row["paid_at"], period_end=end)
            signed = signer.sign("licence_file", terms, now=row["paid_at"])
            with self.write() as conn:
                conn.execute(
                    "UPDATE members SET grant=? WHERE workshop=? AND profile=? AND grant IS NULL",
                    (json.dumps(signed), wid, profile),
                )
        return json.loads(self.member(wid, profile)["grant"])
