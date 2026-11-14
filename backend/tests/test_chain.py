"""Hanu with the NEAR vault switched on, against an in-memory stand-in for the contract.

The stand-in enforces the same roles as contract/src/lib.rs: only the agent files, only
the reviewer decides, and the clinic is paid from the member's balance. The real contract
is tested in contract/src/tests.rs and on testnet by near_setup.py --check.
"""
import pytest

from hanu import chain, db
from test_flow import AMINA, app  # noqa: F401  (the app fixture)

RATE = 10**16


class FakeVault:
    def __init__(self):
        self.balances = {"HN48210937": 42000 * 100 * RATE, "HN11075520": 6000 * 100 * RATE}
        self.claims, self.calls, self.refuse = [], [], None

    def _tx(self, name):
        self.calls.append(name)
        if self.refuse:
            raise chain.ChainError(self.refuse)
        return f"tx{len(self.calls)}"

    def file_claim(self, hanu_id, amount_kobo, clinic, sha, rules_version, flags):
        tx = self._tx("file_claim")
        self.claims.append({"status": "Filed", "hanu_id": hanu_id.replace("-", ""), "flags": flags})
        return {"tx": tx, "value": len(self.claims) - 1}

    def approve(self, claim_id, amount_kobo, clinic, note):
        tx = self._tx("approve")
        claim = self.claims[claim_id]
        assert claim["status"] == "Filed" and clinic.endswith(".hanu.testnet")
        self.balances[claim["hanu_id"]] -= amount_kobo * RATE
        claim.update(status="Approved", clinic=clinic)
        return {"tx": tx, "value": True}

    def deny(self, claim_id, reason):
        tx = self._tx("deny")
        self.claims[claim_id]["status"] = "Denied"
        return {"tx": tx, "value": None}

    def balance_kobo(self, hanu_id):
        return self.balances[hanu_id.replace("-", "")] // RATE


@pytest.fixture(autouse=True)
def vault(monkeypatch):
    monkeypatch.setenv("HANU_VAULT_ACCOUNT", "vault.hanu.testnet")
    monkeypatch.setenv("HANU_AGENT_ACCOUNT", "agent.hanu.testnet")
    monkeypatch.setenv("HANU_REVIEWER_ACCOUNT", "reviewer.hanu.testnet")
    monkeypatch.setenv("HANU_CLINIC_PARENT", "hanu.testnet")
    fake = FakeVault()
    for name in ("file_claim", "approve", "deny", "balance_kobo"):
        monkeypatch.setattr(chain, name, getattr(fake, name))
    return fake


def test_demo_clinics_get_near_accounts(app):
    conn = db.connect()
    try:
        rows = {r["name"]: r["near_account"] for r in conn.execute("SELECT * FROM clinics").fetchall()}
    finally:
        conn.close()
    assert rows["Sabon Gari Family Clinic"] == "sabon-gari-family-clinic.hanu.testnet"


def test_rules_never_pay_on_their_own_when_on_chain(app, vault):
    out = app.submit()  # a clean bill, with shadow mode off
    assert out["status"] == "needs_review"
    assert vault.calls == ["file_claim"]
    case = app.case(out["case_id"])
    assert case["chain"]["claim_id"] == 0
    assert case["chain"]["file_tx_url"].endswith("/txns/tx1")


def test_reviewer_approval_pays_the_clinic_on_chain(app, vault):
    out = app.submit()
    res = app.decide(out["case_id"], action="approve")
    assert res.status_code == 200, res.text
    case = res.json()
    assert case["status"] == "approved"
    assert vault.claims[0]["status"] == "Approved"
    assert vault.claims[0]["clinic"] == "sabon-gari-family-clinic.hanu.testnet"
    assert vault.balance_kobo(AMINA) == 3350000 == case["balance_kobo"]
    assert case["chain"]["decide_tx_url"].endswith("/txns/tx2")
    assert "paid_on_chain" in [a["event"] for a in case["audit"]]


def test_denial_is_recorded_on_chain(app, vault):
    out = app.submit()
    res = app.decide(out["case_id"], action="deny", reason="This is a pharmacy receipt.")
    assert res.status_code == 200, res.text
    assert vault.claims[0]["status"] == "Denied"


def test_chain_refusal_changes_nothing(app, vault):
    out = app.submit()
    vault.refuse = "The member's balance is less than this amount"
    res = app.decide(out["case_id"], action="approve")
    assert res.status_code == 502 and "balance is less" in res.json()["detail"]
    case = app.case(out["case_id"])
    assert case["status"] == "needs_review" and case["balance_kobo"] == 4200000


def test_a_claim_that_failed_to_file_is_filed_at_review(app, vault):
    vault.refuse = "RPC down"
    out = app.submit()
    assert out["status"] == "needs_review"
    assert app.case(out["case_id"])["chain"]["claim_id"] is None
    vault.refuse = None
    assert app.decide(out["case_id"], action="approve").status_code == 200
    assert vault.calls == ["file_claim", "file_claim", "approve"]


def test_member_sees_the_vault(app):
    member = app.anonymous.post("/api/member/login", json={"hanu_id": AMINA, "pin": db.DEMO_PIN}).json()
    me = app.anonymous.get("/api/member/me", headers={"Authorization": "Bearer " + member["token"]}).json()
    assert me["near"]["vault"] == "vault.hanu.testnet"
    assert me["near"]["balance_kobo"] == 4200000


# --- The hand-written client -------------------------------------------------------

def test_base58_round_trip():
    for data in [b"", b"\0\0abc", bytes(range(32))]:
        assert chain.b58decode(chain.b58encode(data)) == data
    assert chain.b58encode(b"\0\x01") == "12"


def test_key_round_trip_and_signature():
    signer = chain.Signer.generate("agent.hanu.testnet")
    again = chain.Signer.from_secret("agent.hanu.testnet", signer.secret)
    assert again.public_key == signer.public_key
    signer.private.public_key().verify(again.private.sign(b"msg"), b"msg")


def test_function_call_key_encoding():
    action = chain.add_function_call_key(b"\x07" * 32, "vault.hanu.testnet", ["file_claim"], 5)
    assert action[0] == 5 and action[1] == 0  # AddKey, ed25519
    assert action[34:42] == b"\0" * 8 and action[42] == 0  # nonce 0, FunctionCall permission
    assert action[43] == 1 and int.from_bytes(action[44:60], "little") == 5  # allowance
    assert action.endswith(b"\x01\0\0\0" + b"\x0a\0\0\0file_claim")
