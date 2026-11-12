"""Generated EVM units `eth-matrix/<chain id>-<case>`: the same requests on every chain, so a change
in ETH code shows up per chain (network name, unit, unknown-chain warnings, token identity).

Expectations for unit X live in scenarios/eth-matrix/X.json (`{"expect": [...]}`, written by
`e2e.py promote`); the steps are only generated here.
"""

import json
from pathlib import Path

# Everything not chain-specific is the same in every unit, so cross-chain diffs only show what the
# firmware does differently per chain.
RECIPIENT = "0xe6ce0a092a99700cd4ccccbb1fedc39cf53e6330"
OWNER = "0x416e88840eb6353e49252da2a2c140ea1f969d1a"  # m/44'/60'/0'/0/0 of the simulator seed
PERMIT2 = "0x000000000022d473030f116ddee9f6b43ac78ba3"  # same address on every chain
# Not in the firmware's ERC-20 registry on any chain.
UNKNOWN_CONTRACT = "0x9c23d67aea7b95d80942e3836bcdf7e708a747c1"

# chain id -> (slip44 coin of the default keypath, Circle USDC contract on that chain).
# 123456 is not in the firmware's network table (unknown-network warnings).
CHAINS = {
    1: (60, "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"),
    10: (60, "0x0b2c639c533813f4aa9d7837caf62653d097ff85"),
    56: (60, "0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d"),
    137: (60, "0x3c499c542cef5e3811e1192ce70d8cc03d5c3359"),
    8453: (60, "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"),
    42161: (60, "0xaf88d065e77c8cc2239327c5edb3a432268e5831"),
    11155111: (1, "0x1c7d4b196cb0c7b01d743fbc6116a902379c7238"),
    123456: (60, None),
}

# Tokens in src/rust/erc20_params/src/tokens.txt with a unique symbol. The registry is mainnet-only
# (`erc20_params::get` returns None for chain_id != 1), so other chains have no erc20-known unit
# and their USDC is the erc20-unknown case.
REGISTRY = {1: "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"}


def erc20_transfer(amount):
    return "a9059cbb" + RECIPIENT[2:].rjust(64, "0") + f"{amount:064x}"


def permit(chain_id, token):
    return {
        "types": {
            "EIP712Domain": [{"name": "name", "type": "string"}, {"name": "version", "type": "string"},
                             {"name": "chainId", "type": "uint256"},
                             {"name": "verifyingContract", "type": "address"}],
            "Permit": [{"name": "owner", "type": "address"}, {"name": "spender", "type": "address"},
                       {"name": "value", "type": "uint256"}, {"name": "nonce", "type": "uint256"},
                       {"name": "deadline", "type": "uint256"}],
        },
        "primaryType": "Permit",
        "domain": {"name": "USD Coin", "version": "2", "chainId": chain_id, "verifyingContract": token},
        "message": {"owner": OWNER, "spender": PERMIT2, "value": "1000000", "nonce": 0, "deadline": 1893456000},
    }


def cases(chain_id):
    """(case, description, step) for one chain."""
    coin, usdc = CHAINS[chain_id]
    kp = f"m/44'/{coin}'/0'/0/0"
    tx = {"chain_id": chain_id, "keypath": kp, "nonce": "05", "gas_limit": "5208",
          "recipient": RECIPIENT, "value": "2bb2c8eabcc000", "data": ""}  # 0.0123 in the native unit
    fee_1559 = {"max_priority_fee_per_gas": "59682f00", "max_fee_per_gas": "06fc23ac00"}  # 1.5 / 30 gwei
    erc20 = {**tx, **fee_1559, "gas_limit": "fde8", "value": "", "data": erc20_transfer(1_000_000)}
    out = [
        ("address", "Display the receive address.",
         {"op": "eth-address", "keypath": kp, "chain_id": chain_id, "display": True}),
        ("legacy", "Legacy native transfer of 0.0123.", {"op": "eth-sign", **tx, "gas_price": "04a817c800"}),
        ("eip1559", "EIP-1559 native transfer of 0.0123.", {"op": "eth-sign-1559", **tx, **fee_1559}),
    ]
    if chain_id in REGISTRY:
        out.append(("erc20-known", "EIP-1559 transfer of 1 USDC, a registry token with a unique symbol.",
                    {"op": "eth-sign-1559", **erc20, "recipient": REGISTRY[chain_id]}))
    unknown = usdc if usdc and chain_id not in REGISTRY else UNKNOWN_CONTRACT
    out += [
        ("erc20-unknown", f"EIP-1559 ERC-20 transfer on contract {unknown}, not in the firmware registry"
         + (" (USDC on this chain)." if unknown == usdc else "."),
         {"op": "eth-sign-1559", **erc20, "recipient": unknown}),
        ("personal-sign", "personal_sign of a sign-in message.",
         {"op": "eth-sign-message", "chain_id": chain_id, "keypath": kp,
          "message": f"Sign in to example.org\nChain ID: {chain_id}\nNonce: 32891756"}),
        ("typed-data", "EIP-712 USDC permit for Permit2.",
         {"op": "eth-sign-typed-data", "chain_id": chain_id, "keypath": kp,
          "typed_data": permit(chain_id, usdc or UNKNOWN_CONTRACT)}),
    ]
    return out


def units(expect_dir):
    out = []
    for chain_id in CHAINS:
        for case, desc, step in cases(chain_id):
            name = f"{chain_id}-{case}"
            p = Path(expect_dir) / f"{name}.json"
            out.append({
                "id": f"eth-matrix/{name}",
                "kind": "scenario",
                "scenario": {"name": f"eth-matrix-{name}", "description": f"Chain {chain_id}: {desc}",
                             "steps": [{**step, "name": case}]},
                "expect": json.loads(p.read_text()).get("expect") if p.exists() else None,
                # e2e.py promote writes the expectation here.
                "path": str(p),
            })
    return out
