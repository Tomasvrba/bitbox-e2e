// Runs a scenario file against a simulator and prints one JSON event per line.
// The orchestrator (run.py) uses the events to label screenshots and to know when a step is done.
//
// Usage: bitbox-e2e-driver <scenario.json> [endpoint]

use bitbox_api::pb;
use bitbox_api::runtime::TokioRuntime;
use serde::Deserialize;
use serde_json::json;

#[derive(Deserialize)]
struct Scenario {
    steps: Vec<Step>,
}

#[derive(Deserialize)]
#[serde(tag = "op", rename_all = "kebab-case")]
enum Step {
    EthAddress {
        name: String,
        chain_id: u64,
        keypath: String,
        #[serde(default)]
        display: bool,
    },
    EthSign {
        name: String,
        chain_id: u64,
        keypath: String,
        nonce: String,
        gas_price: String,
        gas_limit: String,
        recipient: String,
        value: String,
        data: String,
    },
    EthSign1559 {
        name: String,
        chain_id: u64,
        keypath: String,
        nonce: String,
        max_priority_fee_per_gas: String,
        max_fee_per_gas: String,
        gas_limit: String,
        recipient: String,
        value: String,
        data: String,
    },
    EthSignMessage {
        name: String,
        chain_id: u64,
        keypath: String,
        message: String,
    },
}

fn emit(v: serde_json::Value) {
    println!("{v}");
}

// With E2E_HANDSHAKE=1 the harness acknowledges each step-start on stdin before we touch the
// device, so it can attribute simulator output to steps without racing two pipes.
fn start_step(name: &str) {
    emit(json!({ "event": "step-start", "step": name }));
    if std::env::var_os("E2E_HANDSHAKE").is_some() {
        let mut line = String::new();
        std::io::stdin().read_line(&mut line).expect("handshake");
    }
}

// Scenario byte fields are hex, optionally 0x-prefixed. Leading zeroes are stripped because the
// firmware expects minimal big-endian encodings for numeric fields.
fn num(s: &str) -> Vec<u8> {
    let b = hex::decode(s.trim_start_matches("0x")).expect("invalid hex");
    let first = b.iter().position(|&x| x != 0).unwrap_or(b.len());
    b[first..].to_vec()
}

fn bytes(s: &str) -> Vec<u8> {
    hex::decode(s.trim_start_matches("0x")).expect("invalid hex")
}

fn addr(s: &str) -> [u8; 20] {
    bytes(s).try_into().expect("address must be 20 bytes")
}

fn keypath(s: &str) -> bitbox_api::Keypath {
    s.try_into().expect("invalid keypath")
}

async fn run_step(
    bb: &bitbox_api::PairedBitBox<TokioRuntime>,
    step: &Step,
) -> Result<(String, serde_json::Value), bitbox_api::error::Error> {
    Ok(match step {
        Step::EthAddress { name, chain_id, keypath: kp, display } => {
            let a = bb.eth_address(*chain_id, &keypath(kp), *display).await?;
            (name.clone(), json!({ "address": a }))
        }
        Step::EthSign {
            name, chain_id, keypath: kp, nonce, gas_price, gas_limit, recipient, value, data,
        } => {
            let tx = bitbox_api::eth::Transaction {
                nonce: num(nonce),
                gas_price: num(gas_price),
                gas_limit: num(gas_limit),
                recipient: addr(recipient),
                value: num(value),
                data: bytes(data),
            };
            let sig = bb
                .eth_sign_transaction(*chain_id, &keypath(kp), &tx, Some(pb::EthAddressCase::Mixed))
                .await?;
            (name.clone(), json!({ "signature": hex::encode(sig) }))
        }
        Step::EthSign1559 {
            name, chain_id, keypath: kp, nonce, max_priority_fee_per_gas, max_fee_per_gas,
            gas_limit, recipient, value, data,
        } => {
            let tx = bitbox_api::eth::EIP1559Transaction {
                chain_id: *chain_id,
                nonce: num(nonce),
                max_priority_fee_per_gas: num(max_priority_fee_per_gas),
                max_fee_per_gas: num(max_fee_per_gas),
                gas_limit: num(gas_limit),
                recipient: addr(recipient),
                value: num(value),
                data: bytes(data),
            };
            let sig = bb
                .eth_sign_1559_transaction(&keypath(kp), &tx, Some(pb::EthAddressCase::Mixed))
                .await?;
            (name.clone(), json!({ "signature": hex::encode(sig) }))
        }
        Step::EthSignMessage { name, chain_id, keypath: kp, message } => {
            let sig = bb
                .eth_sign_message(*chain_id, &keypath(kp), message.as_bytes())
                .await?;
            (name.clone(), json!({ "signature": hex::encode(sig) }))
        }
    })
}

fn step_name(step: &Step) -> &str {
    match step {
        Step::EthAddress { name, .. }
        | Step::EthSign { name, .. }
        | Step::EthSign1559 { name, .. }
        | Step::EthSignMessage { name, .. } => name,
    }
}

#[tokio::main(flavor = "current_thread")]
async fn main() {
    let args: Vec<String> = std::env::args().collect();
    let scenario: Scenario = serde_json::from_str(
        &std::fs::read_to_string(args.get(1).expect("usage: driver <scenario.json> [endpoint]"))
            .expect("read scenario"),
    )
    .expect("parse scenario");
    let endpoint = args.get(2).map(String::as_str);

    let noise = Box::new(bitbox_api::NoiseConfigNoCache {});
    let bitbox = bitbox_api::BitBox::<TokioRuntime>::from_simulator(endpoint, noise)
        .await
        .expect("connect to simulator");
    start_step("pairing");
    let pairing = bitbox.unlock_and_pair().await.expect("unlock_and_pair");
    emit(json!({ "event": "pairing-code", "code": pairing.get_pairing_code() }));
    let bb = pairing.wait_confirm().await.expect("pairing confirm");
    emit(json!({ "event": "step-end", "step": "pairing", "result": { "version": bb.version().to_string() } }));

    // The headless simulator boots unseeded; the graphical one is started with --preseed.
    let info = bb.device_info().await.expect("device_info");
    if !info.initialized {
        start_step("seed");
        bb.restore_from_mnemonic().await.expect("restore_from_mnemonic");
        emit(json!({ "event": "step-end", "step": "seed", "result": {} }));
    }

    let mut failed = false;
    for step in &scenario.steps {
        start_step(step_name(step));
        match run_step(&bb, step).await {
            Ok((name, result)) => emit(json!({ "event": "step-end", "step": name, "result": result })),
            Err(err) => {
                failed = true;
                emit(json!({ "event": "step-end", "step": step_name(step), "error": err.to_string() }));
            }
        }
    }
    emit(json!({ "event": "done", "ok": !failed }));
    std::process::exit(if failed { 1 } else { 0 });
}
