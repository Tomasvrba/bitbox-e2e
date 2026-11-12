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
    // kebab-case alone would make this `eth-sign1559`.
    #[serde(rename = "eth-sign-1559", alias = "eth-sign1559")]
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
    // `typed_data` is the EIP-712 JSON object as dapps pass it to eth_signTypedData_v4.
    EthSignTypedData {
        name: String,
        chain_id: u64,
        keypath: String,
        typed_data: serde_json::Value,
    },
    // Mirrors bitbox-api-rs tests/test_btc_psbt.rs, so firmware BTC test vectors run unchanged:
    // `registrations`, `force_script_config` and `format_unit` use the vector JSON format.
    // Registrations run first as their own step `<name>-register`; if one fails, signing is skipped.
    BtcSignPsbt {
        name: String,
        coin: Coin,
        psbt: String,
        #[serde(default)]
        registrations: Vec<Registration>,
        force_script_config: Option<ScriptConfigWithKeypath>,
        #[serde(default)]
        format_unit: FormatUnit,
    },
}

#[derive(Deserialize, Clone, Copy)]
#[serde(rename_all = "snake_case")]
enum Coin {
    Btc,
    Tbtc,
    Ltc,
}

#[derive(Deserialize, Default)]
#[serde(rename_all = "snake_case")]
enum FormatUnit {
    #[default]
    Default,
    Sat,
}

#[derive(Deserialize)]
struct Registration {
    script_config: ScriptConfig,
    keypath: Option<String>,
    name: String,
}

#[derive(Deserialize)]
struct ScriptConfigWithKeypath {
    script_config: ScriptConfig,
    keypath: String,
}

#[derive(Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
enum ScriptConfig {
    Simple {
        script_type: String,
    },
    Multisig {
        threshold: u32,
        xpubs: Vec<String>,
        our_xpub_index: u32,
        script_type: String,
    },
    Policy {
        policy: String,
        keys: Vec<KeyOriginInfo>,
    },
}

#[derive(Deserialize)]
struct KeyOriginInfo {
    root_fingerprint: Option<String>,
    keypath: Option<String>,
    xpub: String,
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

fn btc_coin(c: Coin) -> pb::BtcCoin {
    match c {
        Coin::Btc => pb::BtcCoin::Btc,
        Coin::Tbtc => pb::BtcCoin::Tbtc,
        Coin::Ltc => pb::BtcCoin::Ltc,
    }
}

fn script_config(c: &ScriptConfig) -> pb::BtcScriptConfig {
    use pb::btc_script_config::{multisig::ScriptType, SimpleType};
    match c {
        ScriptConfig::Simple { script_type } => bitbox_api::btc::make_script_config_simple(
            match script_type.as_str() {
                "p2wpkh" => SimpleType::P2wpkh,
                "p2wpkh_p2sh" => SimpleType::P2wpkhP2sh,
                "p2tr" => SimpleType::P2tr,
                other => panic!("unknown simple script type {other}"),
            },
        ),
        ScriptConfig::Multisig { threshold, xpubs, our_xpub_index, script_type } => {
            let xpubs: Vec<bitcoin::bip32::Xpub> =
                xpubs.iter().map(|x| x.parse().expect("invalid xpub")).collect();
            bitbox_api::btc::make_script_config_multisig(
                *threshold,
                &xpubs,
                *our_xpub_index,
                match script_type.as_str() {
                    "p2wsh" => ScriptType::P2wsh,
                    "p2wsh_p2sh" => ScriptType::P2wshP2sh,
                    other => panic!("unknown multisig script type {other}"),
                },
            )
        }
        ScriptConfig::Policy { policy, keys } => {
            let keys: Vec<bitbox_api::btc::KeyOriginInfo> = keys
                .iter()
                .map(|k| bitbox_api::btc::KeyOriginInfo {
                    root_fingerprint: k
                        .root_fingerprint
                        .as_deref()
                        .map(|f| f.parse().expect("invalid fingerprint")),
                    keypath: k.keypath.as_deref().map(keypath),
                    xpub: k.xpub.parse().expect("invalid xpub"),
                })
                .collect();
            bitbox_api::btc::make_script_config_policy(policy, &keys)
        }
    }
}

fn signature_count(psbt: &bitcoin::psbt::Psbt) -> usize {
    psbt.inputs
        .iter()
        .map(|i| i.partial_sigs.len() + i.tap_key_sig.is_some() as usize + i.tap_script_sigs.len())
        .sum()
}

// Registers what a btc-sign-psbt step needs, skipping configs that are already registered.
async fn btc_register(
    bb: &bitbox_api::PairedBitBox<TokioRuntime>,
    coin: Coin,
    registrations: &[Registration],
) -> Result<(), bitbox_api::error::Error> {
    for r in registrations {
        let config = script_config(&r.script_config);
        let kp = r.keypath.as_deref().map(keypath);
        if !bb.btc_is_script_config_registered(btc_coin(coin), &config, kp.as_ref()).await? {
            bb.btc_register_script_config(
                btc_coin(coin),
                &config,
                kp.as_ref(),
                pb::btc_register_script_config_request::XPubType::AutoXpubTpub,
                Some(&r.name),
            )
            .await?;
        }
    }
    Ok(())
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
        Step::EthSignTypedData { name, chain_id, keypath: kp, typed_data } => {
            let sig = bb
                .eth_sign_typed_message(*chain_id, &keypath(kp), &typed_data.to_string(), true)
                .await?;
            (name.clone(), json!({ "signature": hex::encode(sig) }))
        }
        Step::BtcSignPsbt { name, coin, psbt, force_script_config, format_unit, .. } => {
            let mut psbt = bitcoin::psbt::Psbt::deserialize(&bytes(psbt)).expect("invalid PSBT");
            let before = signature_count(&psbt);
            let force = force_script_config.as_ref().map(|c| pb::BtcScriptConfigWithKeypath {
                script_config: Some(script_config(&c.script_config)),
                keypath: keypath(&c.keypath).to_vec(),
            });
            let unit = match format_unit {
                FormatUnit::Default => pb::btc_sign_init_request::FormatUnit::Default,
                FormatUnit::Sat => pb::btc_sign_init_request::FormatUnit::Sat,
            };
            bb.btc_sign_psbt(btc_coin(*coin), &mut psbt, force, unit).await?;
            (name.clone(), json!({ "signatures_added": signature_count(&psbt) - before }))
        }
    })
}

fn step_name(step: &Step) -> &str {
    match step {
        Step::EthAddress { name, .. }
        | Step::EthSign { name, .. }
        | Step::EthSign1559 { name, .. }
        | Step::EthSignMessage { name, .. }
        | Step::EthSignTypedData { name, .. }
        | Step::BtcSignPsbt { name, .. } => name,
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
        if let Step::BtcSignPsbt { name, coin, registrations, .. } = step {
            if !registrations.is_empty() {
                let reg = format!("{name}-register");
                start_step(&reg);
                if let Err(err) = btc_register(&bb, *coin, registrations).await {
                    failed = true;
                    emit(json!({ "event": "step-end", "step": reg, "error": err.to_string() }));
                    continue;
                }
                emit(json!({ "event": "step-end", "step": reg, "result": {} }));
            }
        }
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
