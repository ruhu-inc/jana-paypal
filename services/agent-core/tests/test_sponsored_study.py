"""Payment contract and restart tests. The checkout provider is simulated here."""

import secrets
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import httpx
import pytest
from agent_core.config.environments import Environment
from agent_core.store.subscription import verify
from cloud_gateway.config import load_config
from jana_tools.sponsored_check import fixture_members
from jana_tools.sponsored_places import FundingError, Places, amount
from signing_service.hierarchy import dev_signer, dev_trust
from signing_service.keys import Purpose


class Wallet:
    def __init__(self):
        self.authorized = self.voided = self.lost = False
        self.capture = None
        self.unit = None
        self.payer = "BUYER"
        self.mutations = []

    def call(self, method, path, body=None, key=None, raw=None):
        if path == "/v1/notifications/verify-webhook-signature":
            import json

            envelope = json.loads(raw)
            assert envelope["webhook_id"] == "WH-TEST"
            return {"verification_status": "SUCCESS" if envelope["transmission_sig"] == "fixture" else "FAILURE"}
        order = "/v2/checkout/orders"
        auth = "/v2/payments/authorizations/AUTH"
        if method == "POST":
            assert key and key not in [k for _, k in self.mutations]
            self.mutations.append((path, key))
        if method == "POST" and path == order:
            assert body["intent"] == "AUTHORIZE" and len(body["purchase_units"]) == 1
            self.unit = body["purchase_units"][0]
            assert self.unit["amount"] == amount(3600)
            return {
                "id": "ORDER",
                "links": [{"rel": "payer-action", "href": "https://www.sandbox.paypal.com/checkoutnow?token=ORDER"}],
            }
        if path == order + "/ORDER/authorize":
            assert body == {}
            self.authorized = True
            return {}
        if path == auth + "/capture":
            assert self.authorized and not self.voided and not self.capture and body["final_capture"] is True
            self.capture = {
                **body,
                "id": "CAP",
                "status": "COMPLETED",
                "update_time": datetime.now(UTC).isoformat(),
                "supplementary_data": {"related_ids": {"order_id": "ORDER", "authorization_id": "AUTH"}},
            }
            if self.lost:
                raise httpx.ReadTimeout("lost capture response")
            return self.capture
        if path == auth + "/void":
            assert self.authorized and not self.capture and body is None
            self.voided = True
            return {}
        if path == order + "/ORDER":
            payments = {"authorizations": [{"id": "AUTH"}]} if self.authorized else {}
            if self.capture:
                payments["captures"] = [self.capture.copy()]
            return {
                "id": "ORDER",
                "intent": "AUTHORIZE",
                "status": "COMPLETED" if self.authorized else "APPROVED",
                "payer": {"payer_id": self.payer, "address": {"country_code": "US"}},
                "purchase_units": [{**self.unit, "amount": {**amount(3600), "breakdown": {}}, "payments": payments}],
            }
        if path == auth:
            status = "VOIDED" if self.voided else "CAPTURED" if self.capture else "CREATED"
            return {
                "id": "AUTH",
                "amount": amount(3600),
                "status": status,
                "expiration_time": "2099-01-01T00:00:00Z",
                "links": [{"rel": "capture", "href": "https://api-m.sandbox.paypal.com" + auth + "/capture"}],
            }
        assert path == "/v2/payments/captures/CAP" and self.capture
        return self.capture


@pytest.fixture
def workshop(tmp_path):
    clock = [time.time()]
    wallet = Wallet()

    def open_db():
        return Places(
            tmp_path / "places",
            wallet,
            load_config(),
            merchant="MERCHANT",
            payer="BUYER",
            origin="https://sponsor.test",
            clock=lambda: clock[0],
        )

    pay = open_db()
    wid = str(uuid.uuid4())
    members = fixture_members(2)
    pay.register(wid, "sponsor", members, window=30)
    pay.fund(wid)
    return pay, wid, [m[0] for m in members], clock, wallet, open_db


@pytest.mark.parametrize("n", range(4))
def test_exact_capture_or_void_and_signed_profile_months(workshop, n):
    pay, wid, profiles, clock, wallet, reopen = workshop
    pay.authorize(wid)
    for profile in profiles[:n]:
        pay.claim(wid, profile)
    with pytest.raises(FundingError, match="still open"):
        pay.settle(wid, lambda _: True)
    clock[0] += 30
    with pytest.raises(FundingError, match="closed"):
        pay.claim(wid, profiles[0])
    assert reopen().settle(wid, lambda _: True)["frozen"] == n
    if n:
        assert wallet.capture["amount"] == amount(n * 1200)
    else:
        assert wallet.capture is None and wallet.voided
    signer = dev_signer(Purpose.LICENCE)
    subscriptions = set()
    for profile in profiles[:n]:
        grant = pay.grant(wid, profile, signer)
        assert reopen().grant(wid, profile, signer) == grant
        entitlement = verify(grant, Environment.DEV, dev_trust())
        assert entitlement.profile_ids == (profile,) and entitlement.seats == 1
        subscriptions.add(entitlement.subscription_id)
    assert len(subscriptions) == n
    for profile in profiles[n:]:
        with pytest.raises(FundingError, match="no verified payment"):
            pay.grant(wid, profile, signer)
    reopen().settle(wid, lambda _: False)
    assert len(wallet.mutations) == 3


def test_duplicate_claim_withdrawal_coverage_change_and_timeout(workshop):
    pay, wid, profiles, clock, wallet, reopen = workshop
    pay.authorize(wid)
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: pay.claim(wid, profiles[0], key="same"), range(4)))
    pay.claim(wid, profiles[0], withdraw=True, key="withdraw")
    pay.claim(wid, profiles[0], key="same")  # a stale network retry may not undo a later withdrawal
    assert pay.member(wid, profiles[0])["state"] == "withdrawn"
    with pytest.raises(FundingError):
        pay.claim(wid, "foreign")
    for p in profiles[1:]:
        pay.claim(wid, p)
    wallet.lost = True
    clock[0] += 31
    with pytest.raises(httpx.ReadTimeout):
        pay.settle(wid, lambda p: p != profiles[1])
    assert pay.member(wid, profiles[1])["state"] == "excluded"
    assert reopen().settle(wid, lambda _: False)["state"] == "captured"
    assert len(wallet.mutations) == 3 and wallet.capture["amount"] == amount(1200)


def test_foreign_buyer_and_wrong_parent_never_grant(workshop):
    pay, wid, profiles, clock, wallet, _ = workshop
    wallet.payer = "OTHER"
    with pytest.raises(FundingError):
        pay.authorize(wid)
    assert len(wallet.mutations) == 1
    wallet.payer = "BUYER"
    pay.authorize(wid)
    pay.claim(wid, profiles[0])
    wallet.lost = True
    clock[0] += 30
    with pytest.raises(httpx.ReadTimeout):
        pay.settle(wid, lambda _: True)
    wallet.capture["supplementary_data"]["related_ids"]["authorization_id"] = "WRONG"
    with pytest.raises(FundingError):
        pay.settle(wid, lambda _: True)
    with pytest.raises(FundingError):
        pay.grant(wid, profiles[0], None)


def test_unavailable_eligibility_voids_after_bounded_wait(workshop):
    pay, wid, profiles, clock, wallet, _ = workshop
    pay.authorize(wid)
    pay.claim(wid, profiles[0])

    def offline(_):
        raise FundingError("offline")

    clock[0] += 30
    with pytest.raises(FundingError, match="offline"):
        pay.settle(wid, offline)
    assert pay.row(wid)["state"] == "open" and not wallet.capture
    clock[0] += 300
    assert pay.settle(wid, offline)["state"] == "voided"


def test_hosted_sponsor_and_three_personal_study_cores(tmp_path, monkeypatch):
    import asyncio
    import json
    import re
    from types import SimpleNamespace

    from agent_core.config import Settings
    from agent_core.config.trust import TrustStore
    from api_harness import cli, start_core, stop_core
    from jana_tools import hosted
    from jana_tools.hosted_accounts import Accounts, hash_password
    from jana_tools.hosted_sponsorship import Workshop
    from jana_tools.prepaid_signer import initialise
    from net_harness import layer
    from starlette.applications import Starlette
    from workshop_profiles import Api, student
    from workshop_entitlement import entitlement

    async def run():
        origin, sponsor = "https://learn.test", "https://sponsor.test"
        accounts = Accounts(tmp_path / "accounts", [f"member-{n}" for n in range(1, 5)])
        password = secrets.token_urlsafe(20)
        owner = accounts.create("coordinator@example.test", hash_password(password))
        people = [accounts.create(f"adult{i}@example.test") for i in range(3)]
        keydir = tmp_path / "keys"
        cert = initialise(keydir)
        trust = TrustStore.from_document(json.loads((keydir / "anchors.json").read_text()))
        gateway = tmp_path / "gateway.toml"
        gateway.write_text(
            'environment="dev"\nopen_registration=false\nstudy_model="primary/fast-model"\n'
            f'[entitlement_keys]\n"{cert.key_id}"="{cert.public_key}"\n'
            '[slots]\nprimary="test"\n[providers.test]\nadapter="vertex"\n'
            'base_url="https://aiplatform.eu.rep.googleapis.com/v1/projects/workshop-test/locations/eu/publishers/google"\n'
            f'secret="{secrets.token_urlsafe(24)}"\n'
            '[providers.test.models.fast-model]\nid="gemini-3.8-flash"\ncapabilities=["text","structured_output"]\n'
            'context=8192\nmax_output=512\n[prices."gemini-3.8-flash"]\ninput=1.0\ncached_input=0.1\noutput=4.0\n'
        )
        creds = tmp_path / "credentials.toml"
        creds.write_text(f'client_id="{secrets.token_urlsafe(12)}"\nclient_secret="{secrets.token_urlsafe(24)}"')
        creds.chmod(0o600)
        settings = Settings(study_workspace_enabled=True, consent_dpia_ref="fixture")
        wid = str(uuid.uuid4())
        spec = (
            f'database="{tmp_path}/workshops"\ncredentials_file="{creds}"\nworkshop_id="{wid}"\nwindow=15\n'
            f'anchors="{keydir}/anchors.json"\nsponsor_origin="{sponsor}"\nsponsor_account={owner.account_id}\n'
            'merchant="MERCHANT"\npayer="BUYER"\nwebhook_id="WH-TEST"\n'
        )
        cores, running, learners = {}, {}, []
        try:
            for person in people:
                root = tmp_path / person.slot
                r = await start_core(root, settings=settings, worker=False)
                running[person.slot] = r
                r.core.online = layer(root, settings=settings, device=r.runtime.stores.device)
                r.core.entitlement_trust = trust
                api = Api(r)
                profile = await api.add(student("Learner"))
                token = await api.token(profile)
                response = await api.call(
                    "POST",
                    "/v1/study/sources",
                    {"kind": "text", "title": "Motion", "text": "Mean speed equals distance divided by elapsed time."},
                    profile=token,
                )
                assert response.status_code == 201, response.text
                source = response.json()["source_id"]
                spec += (
                    f'[[members]]\naccount_id={person.account_id}\nslot="{person.slot}"\nprofile="{profile}"\n'
                    f'source="{source}"\nrevision=1\nlicence="lic_workshop"\n'
                )
                cores[person.slot] = SimpleNamespace(port=r.port, token=r.token, running=lambda: True, data_dir=root)
                learners.append((person, profile, source, token, api))
            person, profile, _, token, api = learners[-1]
            running[person.slot].core.entitlement_trust = dev_trust()
            assert (
                await api.call(
                    "POST", "/v1/subscription", {"entitlement": entitlement([profile]), "consents": []}, profile=token
                )
            ).is_success
            conf = tmp_path / "sponsored.toml"
            conf.write_text(spec)
            cfg = hosted.Config(
                origin, tmp_path, tmp_path, str(gateway), 8081, True, {}, member_slots=4, sponsored_file=conf
            )
            correct_gateway = gateway.read_text()
            for wrong_gateway in (
                correct_gateway.replace('adapter="vertex"', 'adapter="meta"'),
                correct_gateway.replace("gemini-3.8-flash", "other-model"),
                correct_gateway.replace("[slots]", '[slots]\nfallback="test"'),
            ):
                gateway.write_text(wrong_gateway)
                with pytest.raises(ValueError, match=r"only Vertex Gemini|cannot select a Meta"):
                    Workshop(cfg, accounts, cores, r.client)
            gateway.write_text(correct_gateway)
            workshop = Workshop(cfg, accounts, cores, r.client)
            wallet = Wallet()
            workshop.pay.paypal = wallet
            monkeypatch.setattr(workshop, "service_ready", lambda: True)
            clock = [time.time()]
            workshop.pay.clock = lambda: clock[0]
            app = Starlette(routes=workshop.routes([]))
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as web:
                login = await web.post(
                    sponsor + "/signin",
                    data={"email": owner.email, "password": password},
                    headers={"origin": sponsor},
                )
                assert login.status_code == 303
                offer = await web.get(sponsor + "/")
                assert "$36.00" in offer.text and offer.headers["referrer-policy"] == "same-origin"
                digest = re.search(r'name="offer" value="([^"]+)"', offer.text)[1]
                assert (
                    await web.post(sponsor + "/sponsor/fund", data={"offer": digest}, headers={"origin": origin})
                ).status_code == 403
                result = await web.post(sponsor + "/sponsor/fund", data={"offer": digest}, headers={"origin": sponsor})
                assert result.status_code == 303, result.text
                await asyncio.to_thread(workshop.advance)

                async def action(person, source, token, act=None):
                    web.cookies.set(
                        hosted.SESSION_COOKIE, accounts.start_session(person.account_id), domain="learn.test"
                    )
                    url = origin + "/core/study-place/" + source + "/1"
                    headers = {"origin": origin, "x-profile-token": token, "idempotency-key": cli()}
                    if act:
                        return await web.post(url, json={"action": act}, headers=headers)
                    return await web.get(url, headers=headers)

                for i, (person, _, source, token, _) in enumerate(learners):
                    response = await action(person, source, token)
                    assert response.is_success, response.text
                    assert response.json()["state"] == ("available" if i < 2 else "unavailable")
                    claimed = await action(person, source, token, "claim")
                    assert claimed.status_code == (200 if i < 2 else 409)
                assert (await action(people[0], learners[1][2], learners[1][3], "claim")).status_code >= 400
                clock[0] += 16
                await asyncio.to_thread(workshop.advance)
                sponsor_receipt = await web.get(sponsor + "/")
                assert "2 places funded" in sponsor_receipt.text and "24.00" in sponsor_receipt.text
                assert all(p not in sponsor_receipt.text for _, p, _, _, _ in learners)
                for person, profile, source, token, api in learners[:2]:
                    for _ in range(2):
                        activated = await action(person, source, token, "activate")
                        assert activated.is_success, activated.text
                    plan = (await api.call("GET", "/v1/subscription", profile=token)).json()
                    grant = json.loads(workshop.pay.member(wid, profile)["grant"])
                    assert plan["state"] == "active" and grant["allowance_tokens"] > 0
                    consent = (await api.call("GET", f"/v1/profiles/{profile}/consents", profile=token)).json()
                    assert not any(c["granted"] for c in consent if c["scope"] == "study_materials")
                    original = await api.call("GET", "/v1/study/sources/" + source, profile=token)
                    assert original.json()["revision"] == 1
                hook = sponsor + "/hooks/paypal/sandbox"
                event = {"id": "EV-1", "event_type": "PAYMENT.CAPTURE.REFUNDED", "resource": {"id": "CAP"}}
                headers = {
                    f"paypal-{name}": "fixture"
                    for name in ("auth-algo", "cert-url", "transmission-id", "transmission-sig", "transmission-time")
                }
                assert (await web.post(hook, json=event)).status_code == 400
                assert (
                    await web.post(hook, json=event, headers={**headers, "paypal-transmission-sig": "bad"})
                ).status_code == 403
                for _ in range(2):
                    assert (await web.post(hook, json=event, headers=headers)).status_code == 204
                assert (
                    await web.post(hook, json={**event, "event_type": "changed"}, headers=headers)
                ).status_code == 409
                await asyncio.to_thread(workshop.advance)
                assert workshop.pay.row(wid)["state"] == "captured"  # An event by itself cannot revoke a grant.
                wallet.capture["status"] = "PARTIALLY_REFUNDED"
                assert (await web.post(hook, json={**event, "id": "EV-2"}, headers=headers)).status_code == 204
                await asyncio.to_thread(workshop.advance)
                assert workshop.pay.row(wid)["state"] == "payment_review"
                assert len(wallet.mutations) == 3
        finally:
            for r in running.values():
                if r.core.online:
                    await r.core.online.stop()
                await stop_core(r)

    asyncio.run(run())
