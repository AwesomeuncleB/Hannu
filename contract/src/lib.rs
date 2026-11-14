//! Hanu vault: members' healthcare savings, held on NEAR, paid only to partner clinics.
//!
//! The roles are kept apart by the contract itself, not by the server:
//!
//! - An **agent** (the AI bill reader) can file a claim: what the bill says, the rules'
//!   flags and a fingerprint of the photo. Filing moves no money. The agent's account is
//!   meant to hold only a function-call key limited to `file_claim`, so even a stolen
//!   agent key cannot pay anyone.
//! - A **reviewer** (a person at Hanu) approves or denies a claim. Approving pays the
//!   clinic straight from the member's balance. Denying needs a reason the member is given.
//! - The **owner** manages who the agents, reviewers and partner clinics are. The owner
//!   cannot approve claims unless also added as a reviewer.
//!
//! Money can only leave the vault to a partner clinic, for a filed claim, after a reviewer
//! approves it. There is no withdraw method.
use near_sdk::json_types::U128;
use near_sdk::serde_json::json;
use near_sdk::store::{IterableMap, IterableSet, LookupMap, Vector};
use near_sdk::{env, near, require, AccountId, Gas, NearToken, PanicOnDefault, Promise, PromiseError};

const MIN_REASON_CHARS: usize = 5;
const MAX_TEXT_CHARS: usize = 1000;
const MAX_FLAGS: usize = 20;
const CALLBACK_GAS: Gas = Gas::from_tgas(10);

#[near(serializers = [borsh, json])]
#[derive(Clone, Debug, PartialEq)]
pub enum Status {
    /// Filed by an agent, waiting for a reviewer.
    Filed,
    /// Approved; the transfer to the clinic is in flight.
    Paying,
    Approved,
    Denied,
}

#[near(serializers = [borsh, json])]
#[derive(Clone, Debug)]
pub struct Claim {
    pub id: u64,
    pub hanu_id: String,
    /// What the bill asks for, in yoctoNEAR.
    pub amount: U128,
    /// The clinic the agent matched, if it is a partner.
    pub clinic: Option<AccountId>,
    /// SHA-256 of the bill photo. The photo itself never goes on chain.
    pub bill_sha256: String,
    pub rules_version: String,
    pub flags: Vec<String>,
    pub filed_by: AccountId,
    pub filed_at_ms: u64,
    pub status: Status,
    pub paid: U128,
    pub paid_to: Option<AccountId>,
    pub decided_by: Option<AccountId>,
    /// The reviewer's note on an approval, or the reason given to the member on a denial.
    pub reason: Option<String>,
}

#[near(contract_state)]
#[derive(PanicOnDefault)]
pub struct Vault {
    owner: AccountId,
    agents: IterableSet<AccountId>,
    reviewers: IterableSet<AccountId>,
    /// Partner clinic account -> display name.
    clinics: IterableMap<AccountId, String>,
    /// Hanu ID -> balance in yoctoNEAR.
    balances: LookupMap<String, u128>,
    claims: Vector<Claim>,
    /// Bill photo fingerprint -> claim id, so one photo is one claim.
    by_photo: LookupMap<String, u64>,
}

#[near]
impl Vault {
    #[init]
    pub fn new(owner: AccountId) -> Self {
        Self {
            owner,
            agents: IterableSet::new(b"a"),
            reviewers: IterableSet::new(b"r"),
            clinics: IterableMap::new(b"c"),
            balances: LookupMap::new(b"b"),
            claims: Vector::new(b"l"),
            by_photo: LookupMap::new(b"p"),
        }
    }

    // --- Roles (owner only) --------------------------------------------------------

    pub fn add_agent(&mut self, account_id: AccountId) {
        self.only_owner();
        require!(!self.reviewers.contains(&account_id), "A reviewer cannot also be an agent");
        self.agents.insert(account_id.clone());
        event("agent_added", json!({ "account_id": account_id }));
    }

    pub fn remove_agent(&mut self, account_id: AccountId) {
        self.only_owner();
        self.agents.remove(&account_id);
        event("agent_removed", json!({ "account_id": account_id }));
    }

    pub fn add_reviewer(&mut self, account_id: AccountId) {
        self.only_owner();
        require!(!self.agents.contains(&account_id), "An agent cannot also be a reviewer");
        self.reviewers.insert(account_id.clone());
        event("reviewer_added", json!({ "account_id": account_id }));
    }

    pub fn remove_reviewer(&mut self, account_id: AccountId) {
        self.only_owner();
        self.reviewers.remove(&account_id);
        event("reviewer_removed", json!({ "account_id": account_id }));
    }

    pub fn add_clinic(&mut self, account_id: AccountId, name: String) {
        self.only_owner();
        require!(!name.trim().is_empty() && name.len() <= 200, "Give the clinic a name");
        self.clinics.insert(account_id.clone(), name.clone());
        event("clinic_added", json!({ "account_id": account_id, "name": name }));
    }

    pub fn remove_clinic(&mut self, account_id: AccountId) {
        self.only_owner();
        self.clinics.remove(&account_id);
        event("clinic_removed", json!({ "account_id": account_id }));
    }

    // --- Money in -----------------------------------------------------------------

    /// Add to a member's healthcare balance. Anyone can pay in: the member, a family
    /// member abroad, a cooperative, or an employer.
    #[payable]
    pub fn deposit(&mut self, hanu_id: String) -> U128 {
        let hanu_id = clean_id(&hanu_id);
        let amount = env::attached_deposit().as_yoctonear();
        require!(amount > 0, "Attach the amount to deposit");
        let balance = self.balance(&hanu_id) + amount;
        self.balances.insert(hanu_id.clone(), balance);
        event("deposit", json!({
            "hanu_id": hanu_id, "from": env::predecessor_account_id(),
            "amount": U128(amount), "balance": U128(balance),
        }));
        U128(balance)
    }

    // --- Claims -------------------------------------------------------------------

    /// Record a bill the agent has read. This moves no money.
    pub fn file_claim(
        &mut self,
        hanu_id: String,
        amount: U128,
        clinic: Option<AccountId>,
        bill_sha256: String,
        rules_version: String,
        flags: Vec<String>,
    ) -> u64 {
        require!(self.agents.contains(&env::predecessor_account_id()), "Only an agent can file a claim");
        require!(
            bill_sha256.len() == 64 && bill_sha256.chars().all(|c| c.is_ascii_hexdigit()),
            "bill_sha256 must be a SHA-256 in hex"
        );
        let bill_sha256 = bill_sha256.to_ascii_lowercase();
        require!(self.by_photo.get(&bill_sha256).is_none(), "This bill photo was already filed");
        require!(flags.len() <= MAX_FLAGS && flags.iter().all(|f| f.len() <= MAX_TEXT_CHARS), "Too many or too long flags");
        require!(rules_version.len() <= 100, "rules_version is too long");
        let clinic = clinic.filter(|c| self.clinics.contains_key(c));

        let id = self.claims.len() as u64;
        let claim = Claim {
            id,
            hanu_id: clean_id(&hanu_id),
            amount,
            clinic,
            bill_sha256: bill_sha256.clone(),
            rules_version,
            flags,
            filed_by: env::predecessor_account_id(),
            filed_at_ms: env::block_timestamp_ms(),
            status: Status::Filed,
            paid: U128(0),
            paid_to: None,
            decided_by: None,
            reason: None,
        };
        event("claim_filed", json!({
            "claim_id": id, "hanu_id": claim.hanu_id, "amount": claim.amount,
            "bill_sha256": bill_sha256, "flags": claim.flags.len(), "by": claim.filed_by,
        }));
        self.claims.push(claim);
        self.by_photo.insert(bill_sha256, id);
        id
    }

    /// A reviewer approves a claim: the clinic is paid from the member's balance.
    /// A claim with flags, or a changed amount, needs a note saying why.
    pub fn approve(&mut self, claim_id: u64, amount: U128, clinic: AccountId, note: String) -> Promise {
        let reviewer = self.only_reviewer();
        let amount = amount.0;
        let note = note.trim().to_string();
        require!(note.len() <= MAX_TEXT_CHARS, "The note is too long");
        require!(self.clinics.contains_key(&clinic), "Payments go only to partner clinics");
        require!(amount > 0, "The amount must be more than zero");

        let claim = self.claim_mut(claim_id);
        require!(claim.status == Status::Filed, "This claim has already been decided");
        if !claim.flags.is_empty() || amount != claim.amount.0 {
            require!(note.len() >= MIN_REASON_CHARS, "This claim has flags or a changed amount, so it needs a note");
        }
        let hanu_id = claim.hanu_id.clone();
        claim.status = Status::Paying;
        claim.paid = U128(amount);
        claim.paid_to = Some(clinic.clone());
        claim.decided_by = Some(reviewer.clone());
        claim.reason = Some(note).filter(|n| !n.is_empty());

        let balance = self.balance(&hanu_id);
        require!(amount <= balance, "The member's balance is less than this amount");
        self.balances.insert(hanu_id.clone(), balance - amount);

        event("claim_approved", json!({
            "claim_id": claim_id, "hanu_id": hanu_id, "amount": U128(amount),
            "clinic": clinic, "by": reviewer,
        }));
        Promise::new(clinic)
            .transfer(NearToken::from_yoctonear(amount))
            .then(Self::ext(env::current_account_id()).with_static_gas(CALLBACK_GAS).on_paid(claim_id))
    }

    /// A reviewer refuses a claim. The reason is shown to the member.
    pub fn deny(&mut self, claim_id: u64, reason: String) {
        let reviewer = self.only_reviewer();
        let reason = reason.trim().to_string();
        require!(reason.len() >= MIN_REASON_CHARS, "A denial needs a reason the member can be given");
        require!(reason.len() <= MAX_TEXT_CHARS, "The reason is too long");
        let claim = self.claim_mut(claim_id);
        require!(claim.status == Status::Filed, "This claim has already been decided");
        claim.status = Status::Denied;
        claim.decided_by = Some(reviewer.clone());
        claim.reason = Some(reason.clone());
        event("claim_denied", json!({ "claim_id": claim_id, "reason": reason, "by": reviewer }));
    }

    /// After the transfer: settle the claim, or put the money back if the transfer failed.
    #[private]
    pub fn on_paid(&mut self, claim_id: u64, #[callback_result] result: Result<(), PromiseError>) -> bool {
        let claim = self.claim_mut(claim_id);
        if result.is_ok() {
            claim.status = Status::Approved;
            event("claim_paid", json!({ "claim_id": claim_id, "amount": claim.paid, "clinic": claim.paid_to }));
            return true;
        }
        let (hanu_id, amount) = (claim.hanu_id.clone(), claim.paid.0);
        claim.status = Status::Filed;
        claim.paid = U128(0);
        claim.paid_to = None;
        claim.decided_by = None;
        claim.reason = None;
        let balance = self.balance(&hanu_id) + amount;
        self.balances.insert(hanu_id, balance);
        event("payment_failed", json!({ "claim_id": claim_id, "refunded": U128(amount) }));
        false
    }

    // --- Views --------------------------------------------------------------------

    pub fn get_balance(&self, hanu_id: String) -> U128 {
        U128(self.balance(&clean_id(&hanu_id)))
    }

    pub fn get_claim(&self, claim_id: u64) -> Option<Claim> {
        self.claims.get(claim_id as u32).cloned()
    }

    pub fn get_claims(&self, from_index: Option<u64>, limit: Option<u64>) -> Vec<Claim> {
        let from = from_index.unwrap_or(0) as usize;
        let limit = limit.unwrap_or(50).min(100) as usize;
        self.claims.iter().skip(from).take(limit).cloned().collect()
    }

    pub fn get_claim_count(&self) -> u64 {
        self.claims.len() as u64
    }

    pub fn get_clinics(&self) -> Vec<(AccountId, String)> {
        self.clinics.iter().map(|(a, n)| (a.clone(), n.clone())).collect()
    }

    pub fn get_roles(&self) -> near_sdk::serde_json::Value {
        json!({
            "owner": self.owner,
            "agents": self.agents.iter().collect::<Vec<_>>(),
            "reviewers": self.reviewers.iter().collect::<Vec<_>>(),
        })
    }

    // --- Internals ----------------------------------------------------------------

    fn balance(&self, hanu_id: &str) -> u128 {
        self.balances.get(hanu_id).copied().unwrap_or(0)
    }

    fn claim_mut(&mut self, claim_id: u64) -> &mut Claim {
        self.claims.get_mut(claim_id as u32).unwrap_or_else(|| env::panic_str("No such claim"))
    }

    fn only_owner(&self) {
        require!(env::predecessor_account_id() == self.owner, "Only the owner can do this");
    }

    fn only_reviewer(&self) -> AccountId {
        let caller = env::predecessor_account_id();
        require!(self.reviewers.contains(&caller), "Only a reviewer can decide a claim");
        caller
    }
}

/// Hanu IDs are matched without dashes or case: "hn-4821-0937" is "HN48210937".
fn clean_id(hanu_id: &str) -> String {
    let id: String = hanu_id.chars().filter(|c| c.is_ascii_alphanumeric()).collect::<String>().to_ascii_uppercase();
    require!(!id.is_empty() && id.len() <= 40, "Give a valid Hanu ID");
    id
}

/// A NEP-297 event, so indexers and the explorer can follow every step.
fn event(name: &str, data: near_sdk::serde_json::Value) {
    env::log_str(&format!(
        "EVENT_JSON:{}",
        json!({ "standard": "hanu", "version": "1.0.0", "event": name, "data": [data] })
    ));
}

#[cfg(test)]
mod tests;
