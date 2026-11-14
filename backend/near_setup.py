"""Set up Hanu on NEAR testnet:  python backend/near_setup.py

Needs a funded testnet account in backend/.env (about 6 NEAR; the testnet faucet gives 10):

    NEAR_OPERATOR_ACCOUNT=yourname.testnet
    NEAR_OPERATOR_KEY=ed25519:...         (its full-access secret key)

It creates these sub-accounts of the operator, and writes their settings and keys to
backend/.env.near (not committed), which dev.py reads:

    vault.<operator>      the Hanu vault contract (contract/), owned by the operator
    agent.<operator>      the AI bill reader. Its ONLY key is a function-call key that can
                          call file_claim on the vault and nothing else.
    reviewer.<operator>   signs staff decisions. Its only key can call approve and deny.
    <clinic>.<operator>   one account per demo partner clinic, where payments arrive

Then it registers the roles and clinics, and pays in the demo members' balances. Running
it again skips whatever already exists. Build the contract first:

    cd contract && cargo build --target wasm32-unknown-unknown --release

`--check` runs the safety checks against the deployed vault: the agent's key is refused
when it tries to approve a payment, and a stranger cannot file a claim.
"""
import os
import sys
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
sys.stdout.reconfigure(encoding="utf-8")  # the naira sign, on a Windows console

from dev_env import load_env  # noqa: E402

load_env()

from hanu import chain, config, db  # noqa: E402
from hanu.chain import NEAR_ONE, Signer  # noqa: E402

WASM = HERE.parent / "contract" / "target" / "wasm32-unknown-unknown" / "release" / "hanu_vault.wasm"
OUT = HERE / ".env.near"


def main() -> None:
    if "--create-operator" in sys.argv:
        create_operator(sys.argv[sys.argv.index("--create-operator") + 1])
    op_id, op_key = os.environ.get("NEAR_OPERATOR_ACCOUNT"), os.environ.get("NEAR_OPERATOR_KEY")
    if not op_id or not op_key:
        sys.exit("Set NEAR_OPERATOR_ACCOUNT and NEAR_OPERATOR_KEY in backend/.env first.")
    os.environ.setdefault("HANU_CLINIC_PARENT", op_id)
    op = Signer.from_secret(op_id, op_key)
    saved = _read_saved()
    names = {"vault": f"vault.{op_id}", "agent": f"agent.{op_id}", "reviewer": f"reviewer.{op_id}"}

    if "--check" in sys.argv:
        return check(saved)

    def key_for(role: str) -> Signer:
        env = f"HANU_{role.upper().replace('-', '_')}_KEY"
        account = names.get(role) or role
        return Signer.from_secret(account, saved[env]) if env in saved else Signer.generate(account)

    # The vault: deploy the contract and make the operator its owner.
    vault = key_for("vault")
    if not chain.account_exists(vault.account_id):
        if not WASM.exists():
            sys.exit(f"Build the contract first: {WASM} is missing.")
        code = WASM.read_bytes()
        storage = (len(code) // 100_000 + 2) * NEAR_ONE  # 1 NEAR per 100 kB stored, and room for state
        print(f"Creating {vault.account_id} with {storage // NEAR_ONE} NEAR and deploying {len(code):,} bytes")
        chain.send(op, vault.account_id, [chain.create_account(), chain.transfer(storage),
                                         chain.add_full_access_key(vault.public_bytes)])
        saved["HANU_VAULT_KEY"] = vault.secret
        _write(saved, names)
        chain.send(vault, vault.account_id, [chain.deploy_contract(code),
                                            chain.function_call("new", {"owner": op_id}, gas=50 * chain.TGAS)])

    # The agent and the reviewer each hold one restricted key and nothing else.
    for role, methods in (("agent", ["file_claim"]), ("reviewer", ["approve", "deny"])):
        signer = key_for(role)
        if not chain.account_exists(signer.account_id):
            print(f"Creating {signer.account_id}: its only key can call {', '.join(methods)} on the vault")
            chain.send(op, signer.account_id, [
                chain.create_account(), chain.transfer(NEAR_ONE // 2),
                chain.add_function_call_key(signer.public_bytes, vault.account_id, methods, NEAR_ONE // 4)])
        saved[f"HANU_{role.upper()}_KEY"] = signer.secret
    _write(saved, names)

    # Partner clinics, each with its own account to be paid at.
    for name in db.DEMO_PARTNER_CLINICS:
        account = chain.clinic_account(name)
        env = "HANU_CLINIC_" + account.split(".")[0].upper().replace("-", "_") + "_KEY"
        if not chain.account_exists(account):
            print(f"Creating {account} for {name}")
            clinic = Signer.from_secret(account, saved[env]) if env in saved else Signer.generate(account)
            chain.send(op, account, [chain.create_account(), chain.transfer(NEAR_ONE // 20),
                                     chain.add_full_access_key(clinic.public_bytes)])
            saved[env] = clinic.secret
            _write(saved, names)

    roles = chain.view("get_roles", contract=vault.account_id)
    known = {a for a, _n in chain.view("get_clinics", contract=vault.account_id)}
    actions = []
    if names["agent"] not in roles["agents"]:
        actions.append(chain.function_call("add_agent", {"account_id": names["agent"]}))
    if names["reviewer"] not in roles["reviewers"]:
        actions.append(chain.function_call("add_reviewer", {"account_id": names["reviewer"]}))
    for name in db.DEMO_PARTNER_CLINICS:
        if chain.clinic_account(name) not in known:
            actions.append(chain.function_call("add_clinic", {"account_id": chain.clinic_account(name), "name": name}))
    if actions:
        print(f"Registering {len(actions)} roles and clinics on the vault")
        chain.send(op, vault.account_id, actions)

    # The demo members' starting balances, paid in by the operator.
    os.environ["HANU_VAULT_ACCOUNT"] = vault.account_id
    deposits = []
    for hanu_id, member, naira, _lock in db.DEMO_MEMBERS:
        missing = naira * 100 - chain.balance_kobo(hanu_id)
        if missing > 0:
            print(f"Depositing ₦{missing / 100:,.0f} for {member} ({hanu_id})")
            deposits.append(chain.function_call("deposit", {"hanu_id": hanu_id}, deposit=chain.yocto(missing)))
    if deposits:
        chain.send(op, vault.account_id, deposits)

    print(f"\nDone. Settings are in {OUT.name}. Vault: {chain.account_url(vault.account_id)}")


def create_operator(account_id: str) -> None:
    """Create a new testnet account funded by NEAR's testnet faucet (testnet NEAR has no
    value), and save its key in backend/.env."""
    import httpx

    if config.near_network() != "testnet" or not account_id.endswith(".testnet"):
        sys.exit("--create-operator only works on testnet, with a name ending in .testnet")
    if chain.account_exists(account_id):
        sys.exit(f"{account_id} already exists. Choose another name.")
    signer = Signer.generate(account_id)
    resp = httpx.post("https://helper.nearprotocol.com/account", timeout=60, json={
        "newAccountId": account_id, "newAccountPublicKey": signer.public_key})
    if resp.status_code >= 300:
        sys.exit(f"The testnet faucet refused: {resp.status_code} {resp.text[:300]}")
    with open(HERE / ".env", "a", encoding="utf-8") as f:
        f.write(f"\n# Testnet operator created by near_setup.py\nNEAR_OPERATOR_ACCOUNT={account_id}\n"
                f"NEAR_OPERATOR_KEY={signer.secret}\n")
    os.environ["NEAR_OPERATOR_ACCOUNT"], os.environ["NEAR_OPERATOR_KEY"] = account_id, signer.secret
    print(f"Created {account_id} on testnet; its key is in backend/.env")


def check(saved: dict) -> None:
    """Show that the keys are as limited as the README says."""
    agent = Signer.from_secret(saved["HANU_AGENT_ACCOUNT"], saved["HANU_AGENT_KEY"])
    vault = saved["HANU_VAULT_ACCOUNT"]
    print("1. The agent's key tries to approve a payment ...")
    try:
        chain.send(agent, vault, [chain.function_call("approve", {
            "claim_id": 0, "amount": "1", "clinic": vault, "note": "let me pay"})])
        sys.exit("   FAILED: the agent's key was allowed to call approve.")
    except chain.ChainError as exc:
        print(f"   Refused by NEAR, as it should be: {str(exc)[:160]}")
    print("2. The operator (not an agent) tries to file a claim ...")
    op = Signer.from_secret(os.environ["NEAR_OPERATOR_ACCOUNT"], os.environ["NEAR_OPERATOR_KEY"])
    try:
        chain.send(op, vault, [chain.function_call("file_claim", {
            "hanu_id": "HN-0000-0000", "amount": "1", "clinic": None, "bill_sha256": "0" * 64,
            "rules_version": "check", "flags": []})])
        sys.exit("   FAILED: a non-agent filed a claim.")
    except chain.ChainError as exc:
        print(f"   Refused by the contract, as it should be: {str(exc)[:160]}")
    print(f"3. Roles on the vault: {chain.view('get_roles', contract=vault)}")


def _read_saved() -> dict:
    out = {}
    if OUT.exists():
        for line in OUT.read_text(encoding="utf-8").splitlines():
            key, sep, value = line.partition("=")
            if sep and not key.startswith("#"):
                out[key.strip()] = value.strip()
    return out


def _write(saved: dict, names: dict) -> None:
    saved.update({
        "HANU_VAULT_ACCOUNT": names["vault"], "HANU_AGENT_ACCOUNT": names["agent"],
        "HANU_REVIEWER_ACCOUNT": names["reviewer"], "HANU_CLINIC_PARENT": os.environ["HANU_CLINIC_PARENT"],
        "NEAR_NETWORK": config.near_network(),
    })
    lines = ["# Written by near_setup.py. Holds secret keys: never commit this file."]
    lines += [f"{k}={v}" for k, v in saved.items()]
    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
