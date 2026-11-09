"""Start a disposable local workshop with the real Jana client and a simulated wallet."""

import asyncio, json, secrets, sys, tempfile, time, uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import uvicorn
from starlette.middleware.trustedhost import TrustedHostMiddleware
from agent_core.config import Settings
from agent_core.config.trust import TrustStore
from jana_tools import hosted, hosted_sponsorship
from replay_study import ws as WORKSHEET, import_archive
from jana_tools.hosted_accounts import Accounts, hash_password
from jana_tools.prepaid_signer import initialise

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services/agent-core/tests"))
import api_harness
from agent_core.api.security import APP_ORIGINS
from api_harness import start_core, stop_core
from net_harness import layer
from workshop_profiles import Api, student
from test_sponsored_study import Wallet


async def main():
    with tempfile.TemporaryDirectory(prefix="jana-workshop-") as tmp:
        root = Path(tmp)
        bundle = Path(__file__).resolve().parents[1] / "apps/desktop/dist"
        if not (bundle / "index.html").is_file():
            raise SystemExit("Build the hosted client first. See Run the workshop in README.md.")
        ac = Accounts(root / "accounts", [f"member-{i}" for i in range(1, 5)])
        host = ac.create("coordinator@example.test")
        password = secrets.token_urlsafe(16)
        learner = ac.create("nia@example.test", hash_password(password))
        other = [ac.create(f"demo{i}@example.test") for i in range(2)]
        cert = initialise(root / "keys")
        trustfile = root / "keys/anchors.json"
        gw = root / "gateway.toml"
        gw.write_text(
            'environment="dev"\nopen_registration=false\nstudy_model="primary/fast-model"\n'
            f'[entitlement_keys]\n"{cert.key_id}"="{cert.public_key}"\n'
            '[slots]\nprimary="test"\n[providers.test]\nadapter="vertex"\n'
            'base_url="https://aiplatform.eu.rep.googleapis.com/v1/projects/workshop-test/locations/eu/publishers/google"\n'
            f'secret="{secrets.token_urlsafe(20)}"\n[providers.test.models.fast-model]\nid="gemini-3.8-flash"\n'
            'capabilities=["text","structured_output"]\ncontext=8192\nmax_output=512\n'
            '[prices."gemini-3.8-flash"]\ninput=1.0\ncached_input=0.1\noutput=4.0\n'
        )
        creds = root / "wallet"
        creds.write_text(f'client_id="{secrets.token_urlsafe(12)}"\nclient_secret="{secrets.token_urlsafe(24)}"')
        creds.chmod(0o600)
        s = Settings(study_workspace_enabled=True, consent_dpia_ref="test")
        factory = api_harness.create_app
        with patch.object(
            api_harness,
            "create_app",
            lambda *a, **kw: factory(*a, **kw, origins=APP_ORIGINS | {"http://localhost:8474"}),
        ):
            run = await start_core(root / learner.slot, settings=s, worker=False)
        try:
            run.core.online = layer(root / learner.slot, settings=s, device=run.runtime.stores.device)
            run.core.entitlement_trust = TrustStore.from_document(json.loads(trustfile.read_text()))
            api = Api(run)
            profile = await api.add(student("Nia"))
            token = await api.token(profile)
            resp = await api.call(
                "POST",
                "/v1/study/sources",
                {"kind": "text", "title": "Chemistry workshop: catalysts and equilibrium", "text": WORKSHEET},
                profile=token,
            )
            resp.raise_for_status()
            sid = resp.json()["source_id"]
            wid = str(uuid.uuid4())
            spec = root / "sponsored.toml"
            sponsor = "https://sponsor.test"
            conf = (
                f'database="{root}/workshop.sqlite"\ncredentials_file="{creds}"\nworkshop_id="{wid}"\nwindow=1800\n'
                f'anchors="{trustfile}"\nsponsor_origin="{sponsor}"\nsponsor_account={host.account_id}\nmerchant="MERCHANT"\npayer="BUYER"\n'
            )
            for i, member in enumerate([learner] + other):
                pr = profile if i == 0 else "prf_" + str(i) * 26
                so = sid if i == 0 else "src_" + str(i) * 26
                conf += f'[[members]]\naccount_id={member.account_id}\nslot="{member.slot}"\nprofile="{pr}"\nsource="{so}"\nrevision=1\nlicence="lic_workshop"\n'
            spec.write_text(conf)
            cfg = hosted.Config(
                "http://localhost:8474",
                bundle,
                root,
                str(gw),
                8081,
                secure_cookie=True,
                slots={},
                member_slots=4,
                accounts_db=root / "accounts",
                sponsored_file=spec,
            )
            make = hosted_sponsorship.Workshop

            def setup(*args, **kwargs):
                w = make(*args, **kwargs)
                w.pay.paypal = Wallet()
                w.service_ready = lambda: True
                w.pay.fund(w.wid)
                w.pay.authorize(w.wid)

                async def poll():
                    restored = False
                    while True:
                        await asyncio.sleep(0.5)
                        if w.pay.member(wid, profile)["state"] == "claimed" and w.pay.row(wid)["state"] == "open":
                            await asyncio.sleep(3)
                            now = time.time() + 1900
                            w.pay.clock = lambda: now
                        await asyncio.to_thread(w.advance)
                        if not restored and w.pay.member(wid, profile)["state"] == "applied":
                            restored = True
                            import_archive(root / learner.slot, profile, sid, s)

                w.poll = poll
                return w

            with patch.object(hosted_sponsorship, "Workshop", setup):
                app = hosted.create_app(cfg)
            app.state.cores[learner.slot] = SimpleNamespace(
                port=run.port,
                token=run.token,
                running=lambda: True,
                ensure=AsyncMock(),
                stop=AsyncMock(),
                data_dir=root / learner.slot,
            )
            app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost"])
            print(
                "Local rehearsal: simulated PayPal settlement and previously saved Gemini answers. No external service calls.",
                flush=True,
            )
            print(
                "Open http://localhost:8474 and sign in as nia@example.test with this temporary password:",
                password,
                flush=True,
            )
            await uvicorn.Server(
                uvicorn.Config(app, host="127.0.0.1", port=8474, log_level="warning", access_log=False)
            ).serve()
        finally:
            await run.core.online.stop()
            await stop_core(run)


if __name__ == "__main__":
    asyncio.run(main())
