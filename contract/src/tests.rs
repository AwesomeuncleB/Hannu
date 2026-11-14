use super::*;
use near_sdk::test_utils::{get_logs, VMContextBuilder};
use near_sdk::{testing_env, PromiseResult};

const SHA: &str = "aa00000000000000000000000000000000000000000000000000000000000001";

fn acc(name: &str) -> AccountId {
    name.parse().unwrap()
}

fn as_caller(name: &str, deposit: u128) {
    testing_env!(VMContextBuilder::new()
        .current_account_id(acc("vault.hanu.testnet"))
        .predecessor_account_id(acc(name))
        .attached_deposit(NearToken::from_yoctonear(deposit))
        .build());
}

/// A vault with one agent, one reviewer, one partner clinic and a member holding 1000.
fn setup() -> Vault {
    as_caller("hanu.testnet", 0);
    let mut v = Vault::new(acc("hanu.testnet"));
    v.add_agent(acc("agent.hanu.testnet"));
    v.add_reviewer(acc("reviewer.hanu.testnet"));
    v.add_clinic(acc("clinic.hanu.testnet"), "Sabon Gari Family Clinic".into());
    as_caller("family.testnet", 1000);
    v.deposit("HN-4821-0937".into());
    v
}

fn file(v: &mut Vault, amount: u128, flags: Vec<String>) -> u64 {
    as_caller("agent.hanu.testnet", 0);
    v.file_claim("hn48210937".into(), U128(amount), Some(acc("clinic.hanu.testnet")), SHA.into(), "r1".into(), flags)
}

fn settle(v: &mut Vault, id: u64, ok: bool) -> bool {
    let result = if ok { PromiseResult::Successful(vec![]) } else { PromiseResult::Failed };
    testing_env!(
        VMContextBuilder::new()
            .current_account_id(acc("vault.hanu.testnet"))
            .predecessor_account_id(acc("vault.hanu.testnet"))
            .build(),
        near_sdk::test_vm_config(),
        near_sdk::RuntimeFeesConfig::test(),
        Default::default(),
        vec![result]
    );
    v.on_paid(id, if ok { Ok(()) } else { Err(PromiseError::Failed) })
}

#[test]
fn deposit_matches_ids_without_dashes() {
    let v = setup();
    assert_eq!(v.get_balance("hn-4821-0937".into()).0, 1000);
    assert!(get_logs().iter().any(|l| l.contains("\"event\":\"deposit\"")));
}

#[test]
fn clean_claim_is_paid_and_debited() {
    let mut v = setup();
    let id = file(&mut v, 400, vec![]);
    as_caller("reviewer.hanu.testnet", 0);
    v.approve(id, U128(400), acc("clinic.hanu.testnet"), "".into());
    assert_eq!(v.get_claim(id).unwrap().status, Status::Paying);
    assert_eq!(v.get_balance("HN48210937".into()).0, 600);
    assert!(settle(&mut v, id, true));
    let claim = v.get_claim(id).unwrap();
    assert_eq!(claim.status, Status::Approved);
    assert_eq!(claim.decided_by, Some(acc("reviewer.hanu.testnet")));
}

#[test]
fn failed_transfer_puts_the_money_back() {
    let mut v = setup();
    let id = file(&mut v, 400, vec![]);
    as_caller("reviewer.hanu.testnet", 0);
    v.approve(id, U128(400), acc("clinic.hanu.testnet"), "".into());
    assert!(!settle(&mut v, id, false));
    assert_eq!(v.get_balance("HN48210937".into()).0, 1000);
    assert_eq!(v.get_claim(id).unwrap().status, Status::Filed);
}

#[test]
#[should_panic(expected = "Only a reviewer can decide a claim")]
fn agent_cannot_approve() {
    let mut v = setup();
    let id = file(&mut v, 400, vec![]);
    as_caller("agent.hanu.testnet", 0);
    v.approve(id, U128(400), acc("clinic.hanu.testnet"), "".into());
}

#[test]
#[should_panic(expected = "Only a reviewer can decide a claim")]
fn owner_cannot_approve() {
    let mut v = setup();
    let id = file(&mut v, 400, vec![]);
    as_caller("hanu.testnet", 0);
    v.approve(id, U128(400), acc("clinic.hanu.testnet"), "".into());
}

#[test]
#[should_panic(expected = "Only an agent can file a claim")]
fn stranger_cannot_file() {
    let mut v = setup();
    as_caller("someone.testnet", 0);
    v.file_claim("HN48210937".into(), U128(1), None, SHA.into(), "r1".into(), vec![]);
}

#[test]
#[should_panic(expected = "Payments go only to partner clinics")]
fn cannot_pay_a_non_partner() {
    let mut v = setup();
    let id = file(&mut v, 400, vec![]);
    as_caller("reviewer.hanu.testnet", 0);
    v.approve(id, U128(400), acc("reviewer.hanu.testnet"), "pay me".into());
}

#[test]
#[should_panic(expected = "balance is less than this amount")]
fn cannot_pay_more_than_the_balance() {
    let mut v = setup();
    let id = file(&mut v, 5000, vec![]);
    as_caller("reviewer.hanu.testnet", 0);
    v.approve(id, U128(5000), acc("clinic.hanu.testnet"), "".into());
}

#[test]
#[should_panic(expected = "needs a note")]
fn flagged_claim_needs_a_note() {
    let mut v = setup();
    let id = file(&mut v, 400, vec!["Clinic name does not match".into()]);
    as_caller("reviewer.hanu.testnet", 0);
    v.approve(id, U128(400), acc("clinic.hanu.testnet"), "".into());
}

#[test]
#[should_panic(expected = "already been decided")]
fn cannot_pay_twice() {
    let mut v = setup();
    let id = file(&mut v, 400, vec![]);
    as_caller("reviewer.hanu.testnet", 0);
    v.approve(id, U128(400), acc("clinic.hanu.testnet"), "".into());
    v.approve(id, U128(400), acc("clinic.hanu.testnet"), "".into());
}

#[test]
#[should_panic(expected = "already filed")]
fn same_photo_is_one_claim() {
    let mut v = setup();
    file(&mut v, 400, vec![]);
    file(&mut v, 400, vec![]);
}

#[test]
fn denial_records_the_reason() {
    let mut v = setup();
    let id = file(&mut v, 400, vec![]);
    as_caller("reviewer.hanu.testnet", 0);
    v.deny(id, "The bill is for a pharmacy, not a consultation.".into());
    let claim = v.get_claim(id).unwrap();
    assert_eq!(claim.status, Status::Denied);
    assert!(claim.reason.unwrap().contains("pharmacy"));
    assert_eq!(v.get_balance("HN48210937".into()).0, 1000);
}

#[test]
#[should_panic(expected = "needs a reason")]
fn denial_needs_a_reason() {
    let mut v = setup();
    let id = file(&mut v, 400, vec![]);
    as_caller("reviewer.hanu.testnet", 0);
    v.deny(id, "no".into());
}

#[test]
#[should_panic(expected = "An agent cannot also be a reviewer")]
fn roles_stay_apart() {
    let mut v = setup();
    as_caller("hanu.testnet", 0);
    v.add_reviewer(acc("agent.hanu.testnet"));
}
