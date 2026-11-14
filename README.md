# Hanu

Hanu is a healthcare savings and payment platform. It is not insurance. People save for
their own healthcare, alone or through a cooperative, and Hanu pays partner clinics
directly for consultations and lab tests.

AI agents read each bill and flag anything that looks wrong. Written rules judge the
result, and only a person can refuse a payment.

This repository holds an early prototype.

## Hanu on NEAR (NEAR Hackathon, November 2026)

**Tracks:** Real-World Money (main), AI Security & Defense.

Hanu's promise is that AI reads each bill, written rules judge it, and only a person can
pay or refuse it. Before the hackathon the server enforced that. On NEAR, a contract does:
the members' savings sit in a vault contract, and the keys are split so the AI agent
**cannot** move money even if its key is stolen.

| Account | What its key can do |
|---|---|
| `vault.<operator>` | The vault contract ([contract/src/lib.rs](contract/src/lib.rs)). Holds each member's balance. Has no withdraw method: money leaves only to a partner clinic, for a filed claim, after a reviewer approves it. |
| `agent.<operator>` | The AI bill reader. Its only key is a NEAR **function-call key** limited to `file_claim` on the vault, with a small gas allowance. Filing records the bill's amount, flags and a SHA-256 of the photo, and moves no money. |
| `reviewer.<operator>` | Signs staff decisions. Its only key can call `approve` and `deny`. Approving pays the clinic in the same transaction; a failed transfer puts the money back. A denial must carry a reason, which is recorded on chain and shown to the member. |
| `<clinic>.<operator>` | Each partner clinic has its own account and is paid there directly. |
| operator | Owns the vault: adds agents, reviewers and clinics. It cannot approve claims, and an account cannot be both agent and reviewer. |

Anyone can pay into a member's balance with `deposit(hanu_id)`: the member, family
abroad, a cooperative, an employer. Every step emits a NEP-297 event, so the audit trail
can be followed on the explorer. Bill photos and names never go on chain.

When NEAR is switched on, the flow is:

1. A member sends a photo. The reader and the rules run as before, and the case is saved.
2. The agent account files the claim on NEAR. If NEAR is unreachable, the bill is still
   saved and the claim is filed when a reviewer decides.
3. A reviewer approves or denies in the review queue, and the reviewer account signs the
   decision. If the contract refuses (for example, the balance is too low), nothing is
   recorded in Hanu either.
4. The member page shows the vault balance and a link to the payment on the explorer.

On chain, the rules never pay on their own, even with shadow mode off: only a reviewer
moves money.

### Live on testnet

- Vault: [vault.hanu-vault-demo.testnet](https://testnet.nearblocks.io/address/vault.hanu-vault-demo.testnet)
- Agent (one key, `file_claim` only): [agent.hanu-vault-demo.testnet](https://testnet.nearblocks.io/address/agent.hanu-vault-demo.testnet)
- Reviewer (one key, `approve` and `deny` only): [reviewer.hanu-vault-demo.testnet](https://testnet.nearblocks.io/address/reviewer.hanu-vault-demo.testnet)
- A bill filed by the agent: [claim #0](https://testnet.nearblocks.io/txns/4n315uMyEzewBHkeL7KSwyr9pBWFG6nWyPKiV4CpFGda),
  then approved by the reviewer and paid to the clinic: [payment](https://testnet.nearblocks.io/txns/2VvLzjDvb29G6oD3z2wAa7LbCqZbRtBL1jsmZXKJ3kEp)
- The contract refusing a non-agent's claim: [transaction](https://testnet.nearblocks.io/txns/5AwUCUwU5Vd1mMv7uUQ9A7yA4Mr9skNQk184jkHMDu7E).
  The agent's key trying to call `approve` is rejected by NEAR before it reaches the contract
  (`MethodNameMismatch`), so that attempt never becomes a transaction; `near_setup.py --check` shows it.

### What was built during the hackathon

All of Hanu was built inside the hackathon window, starting 11 November 2026. The tag `pre-hackathon` marks the prototype before NEAR was added, and `git diff pre-hackathon hackathon-submission` shows the NEAR work.

### Run it on testnet

1. Build the contract (Rust 1.81 is pinned in `contract/rust-toolchain.toml`, because later
   versions emit WebAssembly features NEAR does not accept yet):

   ```bash
   cd contract && cargo build --target wasm32-unknown-unknown --release
   ```

2. Put a funded testnet account in `backend/.env` as `NEAR_OPERATOR_ACCOUNT` and
   `NEAR_OPERATOR_KEY` (about 6 testnet NEAR). Or let the script create one from the
   testnet faucet: `python backend/near_setup.py --create-operator yourname.testnet`.
3. Set everything up, then run the safety checks:

   ```bash
   python backend/near_setup.py
   python backend/near_setup.py --check
   ```

   This writes the accounts and keys to `backend/.env.near` (not committed).
4. Run Hanu as usual with `python backend/dev.py`. It reads `.env.near` and turns NEAR on.

Amounts are converted at a fixed demo rate (`HANU_YOCTO_PER_KOBO`, default 1 NEAR =
₦1,000,000), and balances are held in testnet NEAR.

### Not built yet

- Stablecoin balances (NEP-141 USDC) instead of testnet NEAR.
- Paying in from other chains with NEAR Intents.
- One NEAR key per member of staff. Today all decisions are signed by one reviewer account,
  and Hanu's own sign-in records which person made each one.
- Reading bills with private inference on NEAR AI Cloud.

### Tools and generated code

Libraries: near-sdk (Rust), FastAPI, cryptography (ed25519), httpx, Pillow, psycopg, Groq.
Some of the code in this repository was written with an AI coding assistant (Antigravity & Claude) and reviewed
by the author.

## What is here

One service (`backend/`) serves everything:

| Page | Path | Who it is for |
|---|---|---|
| Landing page | `/` | Everyone |
| Member page | `/member` | Members: create a Hanu ID, sign in, see balance, send a bill |
| Review queue | `/review` | Staff: sign in, see each bill, approve or deny |

Behind the pages:

- **Bill reader** (`hanu/extract.py`): a vision model on Groq turns a photo of a clinic
  bill into structured data. It only transcribes; it cannot approve, pay or deny.
- **Rules** (`hanu/rules.py`): written, versioned checks that either approve a bill or
  refer it to a person. The rules never deny.
- **Review and payment** (`hanu/service.py`): a signed-in member of staff approves or
  denies. A denial always needs a reason, which the member sees. Every step is written to
  an audit log.
- **Storage** (`hanu/db.py`): members, staff, clinics, the ledger, cases and bill photos.
  Postgres when `DATABASE_URL` is set, a local SQLite file otherwise.

## What is not built yet

- The price agent and the reminder agent shown on the landing page.
- Real payouts to clinics without NEAR. With NEAR switched on (see above), the vault pays the clinic's
  testnet account; without it, approving a bill debits the member's balance and records what the clinic is owed.
- Collecting contributions, cooperatives and groups. A new member starts with a zero balance.
- Phone verification at sign-up, PIN reset, and more than one staff account from the interface.
- WhatsApp. Members use the mobile page.

## Run it locally

```bash
pip install -r backend/requirements.txt
python backend/dev.py
```

Then open http://127.0.0.1:8000. This uses a local SQLite file and loads invented demo
members and clinics. The demo members' PIN is `DEMO_PIN` in `backend/hanu/db.py`, and the
local staff account is set at the top of `backend/dev.py`.

Settings go in `backend/.env` (not committed), one `KEY=value` per line. All of them are
listed at the top of `backend/hanu/config.py`. Without `GROQ_API_KEY` the service uses a
mock reader that returns the same canned bill for every photo.

By default the service runs in shadow mode: every bill goes to a person, and the rules
only recommend.

## Deploying

`pyproject.toml` points Vercel at the app. Set these environment variables in the project:

| Variable | What it is |
|---|---|
| `DATABASE_URL` | Postgres connection string, for example from Neon. Tables are created on first start. |
| `GROQ_API_KEY` | Key for the bill reader. |
| `HANU_ADMIN_EMAIL`, `HANU_ADMIN_PASSWORD` | The first staff account, created on first start. |
| `HANU_SEED_DEMO` | Set to `1` only for a demo: it adds demo members whose PIN is public in this repository. |

Without `DATABASE_URL`, a Vercel deployment keeps its data on a temporary disk that is
wiped whenever the function restarts.

## Tests

```bash
cd backend
python -m pytest -q
```

Set `HANU_TEST_DATABASE_URL` to run the same tests on Postgres. Every table in that
database is dropped first, so point it at a throwaway database only.

The contract's tests need Linux or macOS (NEAR's test runtime does not build on Windows);
GitHub Actions runs them on every push:

```bash
cd contract
cargo test
```
