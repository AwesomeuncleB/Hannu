"""NEAR: the Hanu vault contract, which holds members' balances and pays clinics.

When HANU_VAULT_ACCOUNT is set, every bill is filed on chain by the agent account and
every decision is made on chain by the reviewer account. The two accounts have different
keys, and the contract only lets the reviewer move money. When it is not set, Hanu runs
as before, on its own database only.

This is a small client written for Hanu: JSON-RPC views, and signed function-call
transactions encoded in Borsh by hand. It needs no NEAR SDK, so it runs on Vercel.
"""
import base64
import hashlib
import json
from dataclasses import dataclass

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from . import config

TGAS = 10**12
YOCTO_PER_NEAR = NEAR_ONE = 10**24


class ChainError(Exception):
    """A NEAR call that failed. The message is safe to show to staff."""


# --- Base58 ------------------------------------------------------------------------

_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def b58encode(data: bytes) -> str:
    n = int.from_bytes(data, "big")
    out = ""
    while n:
        n, r = divmod(n, 58)
        out = _B58[r] + out
    return "1" * (len(data) - len(data.lstrip(b"\0"))) + out


def b58decode(text: str) -> bytes:
    n = 0
    for ch in text:
        n = n * 58 + _B58.index(ch)
    body = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    return b"\0" * (len(text) - len(text.lstrip("1"))) + body


# --- Keys --------------------------------------------------------------------------

@dataclass
class Signer:
    account_id: str
    private: Ed25519PrivateKey

    @classmethod
    def from_secret(cls, account_id: str, secret: str) -> "Signer":
        """secret is "ed25519:<base58>", as near-cli writes it (64 bytes: seed then public key)."""
        raw = b58decode(secret.split(":", 1)[-1])
        return cls(account_id, Ed25519PrivateKey.from_private_bytes(raw[:32]))

    @classmethod
    def generate(cls, account_id: str) -> "Signer":
        return cls(account_id, Ed25519PrivateKey.generate())

    @property
    def public_bytes(self) -> bytes:
        return self.private.public_key().public_bytes_raw()

    @property
    def public_key(self) -> str:
        return "ed25519:" + b58encode(self.public_bytes)

    @property
    def secret(self) -> str:
        return "ed25519:" + b58encode(self.private.private_bytes_raw() + self.public_bytes)


# --- Borsh -------------------------------------------------------------------------

def _u8(n): return n.to_bytes(1, "little")
def _u32(n): return n.to_bytes(4, "little")
def _u64(n): return n.to_bytes(8, "little")
def _u128(n): return n.to_bytes(16, "little")
def _str(s: str): return _u32(len(s.encode())) + s.encode()
def _bytes(b: bytes): return _u32(len(b)) + b
def _pk(raw: bytes): return _u8(0) + raw  # 0 = ed25519


def create_account() -> bytes:
    return _u8(0)


def deploy_contract(code: bytes) -> bytes:
    return _u8(1) + _bytes(code)


def function_call(method: str, args: dict, gas: int = 30 * TGAS, deposit: int = 0) -> bytes:
    return _u8(2) + _str(method) + _bytes(json.dumps(args).encode()) + _u64(gas) + _u128(deposit)


def transfer(amount: int) -> bytes:
    return _u8(3) + _u128(amount)


def add_full_access_key(public_key: bytes) -> bytes:
    return _u8(5) + _pk(public_key) + _u64(0) + _u8(1)


def add_function_call_key(public_key: bytes, receiver: str, methods: list[str], allowance: int | None) -> bytes:
    """A key that can only call these methods on this contract, attach no deposit, and
    spend at most `allowance` yoctoNEAR on gas."""
    allowance_b = _u8(1) + _u128(allowance) if allowance is not None else _u8(0)
    methods_b = _u32(len(methods)) + b"".join(_str(m) for m in methods)
    return _u8(5) + _pk(public_key) + _u64(0) + _u8(0) + allowance_b + _str(receiver) + methods_b


# --- RPC ---------------------------------------------------------------------------

def _rpc(method: str, params) -> dict:
    try:
        resp = httpx.post(config.near_rpc_url(), timeout=60, json={
            "jsonrpc": "2.0", "id": "hanu", "method": method, "params": params})
        body = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise ChainError(f"NEAR RPC could not be reached: {exc}") from exc
    if "error" in body:
        err = body["error"]
        raise ChainError(json.dumps(err.get("data") or err.get("cause") or err)[:500])
    return body["result"]


def view(method: str, args: dict | None = None, contract: str | None = None):
    result = _rpc("query", {
        "request_type": "call_function", "finality": "final",
        "account_id": contract or config.vault_account(), "method_name": method,
        "args_base64": base64.b64encode(json.dumps(args or {}).encode()).decode()})
    return json.loads(bytes(result["result"]).decode() or "null")


def account_exists(account_id: str) -> bool:
    try:
        _rpc("query", {"request_type": "view_account", "finality": "final", "account_id": account_id})
        return True
    except ChainError:
        return False


def send(signer: Signer, receiver: str, actions: list[bytes]) -> dict:
    """Sign and send one transaction, wait until it and its receipts have run, and return
    {"tx": hash, "value": decoded return value}. Raises ChainError if anything failed."""
    for attempt in range(2):
        key = _rpc("query", {"request_type": "view_access_key", "finality": "final",
                             "account_id": signer.account_id, "public_key": signer.public_key})
        tx = (_str(signer.account_id) + _pk(signer.public_bytes) + _u64(key["nonce"] + 1)
              + _str(receiver) + b58decode(key["block_hash"]) + _u32(len(actions)) + b"".join(actions))
        digest = hashlib.sha256(tx).digest()
        signed = tx + _u8(0) + signer.private.sign(digest)
        try:
            outcome = _rpc("broadcast_tx_commit", [base64.b64encode(signed).decode()])
            break
        except ChainError as exc:
            if attempt == 0 and "InvalidNonce" in str(exc):
                continue  # another request used the key at the same moment
            raise
    tx_hash = b58encode(digest)
    failure = _first_failure(outcome)
    if failure:
        raise ChainError(f"{failure} (transaction {tx_hash})")
    raw = outcome["status"].get("SuccessValue", "")
    value = json.loads(base64.b64decode(raw) or b"null") if raw else None
    return {"tx": tx_hash, "value": value}


def _first_failure(outcome: dict) -> str | None:
    for item in [outcome["transaction_outcome"], *outcome.get("receipts_outcome", [])]:
        status = item["outcome"]["status"]
        if "Failure" in status:
            err = status["Failure"]
            panic = (err.get("ActionError", {}).get("kind", {}).get("FunctionCallError", {})
                     .get("ExecutionError"))
            return panic or json.dumps(err)[:300]
    if "Failure" in outcome["status"]:
        return json.dumps(outcome["status"]["Failure"])[:300]
    return None


# --- Hanu vault --------------------------------------------------------------------

def enabled() -> bool:
    return bool(config.vault_account())


def yocto(kobo: int) -> int:
    return int(kobo) * config.yocto_per_kobo()


def kobo_from_yocto(amount) -> int:
    return int(amount) // config.yocto_per_kobo()


def tx_url(tx_hash: str | None) -> str | None:
    return f"{config.near_explorer_url()}/txns/{tx_hash}" if tx_hash else None


def account_url(account_id: str) -> str:
    return f"{config.near_explorer_url()}/address/{account_id}"


def _agent() -> Signer:
    return Signer.from_secret(config.agent_account(), config.agent_key())


def _reviewer() -> Signer:
    return Signer.from_secret(config.reviewer_account(), config.reviewer_key())


def file_claim(hanu_id: str, amount_kobo: int, clinic: str | None, bill_sha256: str,
               rules_version: str, flags: list[str]) -> dict:
    """Signed by the agent. Its key can call this method and nothing else."""
    return send(_agent(), config.vault_account(), [function_call("file_claim", {
        "hanu_id": hanu_id, "amount": str(yocto(amount_kobo)), "clinic": clinic,
        "bill_sha256": bill_sha256, "rules_version": rules_version, "flags": flags,
    })])


def approve(claim_id: int, amount_kobo: int, clinic: str, note: str) -> dict:
    """Signed by the reviewer: pays the clinic from the member's balance on chain."""
    result = send(_reviewer(), config.vault_account(), [function_call("approve", {
        "claim_id": claim_id, "amount": str(yocto(amount_kobo)), "clinic": clinic, "note": note,
    }, gas=60 * TGAS)])
    if result["value"] is not True:
        raise ChainError(f"The transfer to {clinic} failed and the balance was restored "
                         f"(transaction {result['tx']})")
    return result


def deny(claim_id: int, reason: str) -> dict:
    return send(_reviewer(), config.vault_account(), [function_call("deny", {
        "claim_id": claim_id, "reason": reason})])


def balance_kobo(hanu_id: str) -> int:
    return kobo_from_yocto(view("get_balance", {"hanu_id": hanu_id}))


def clinic_account(name: str) -> str | None:
    """The NEAR account a demo clinic is paid at: <slug>.<HANU_CLINIC_PARENT>."""
    parent = config.clinic_parent()
    if not parent:
        return None
    slug = "-".join("".join(c if c.isalnum() else " " for c in name.lower()).split())[:40]
    return f"{slug}.{parent}"
