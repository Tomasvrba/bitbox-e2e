"""Units: one scenario run each, from scenarios/*.json, the generated EVM matrix (eth_matrix.py)
and the firmware's BTC test vectors.

  units.py <firmware_dir> [ref]                       list unit ids
  units.py <firmware_dir> [ref] --scenario <unit-id>  print its scenario (for e2e.py compare/device)
"""

import json
import re
import subprocess
import sys
from pathlib import Path

import eth_matrix

VECTORS_PATH = "src/rust/bitbox-test-vectors/testdata/btc-transaction-test-vectors.json"
# Step name of the signing call in vector units; expect.py compares only its screens.
VECTOR_SIGN_STEP = "sign"


def slug(unit_id):
    return re.sub(r"[^A-Za-z0-9._-]", "_", unit_id.replace("/", "--"))


def read_vectors(firmware_dir, ref=None):
    if ref is None:
        p = Path(firmware_dir) / VECTORS_PATH
        return json.loads(p.read_text()) if p.exists() else None
    res = subprocess.run(["git", "-C", str(firmware_dir), "show", f"{ref}:{VECTORS_PATH}"],
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    return json.loads(res.stdout) if res.returncode == 0 else None


def vector_unit(v):
    opts = v["psbt"].get("options", {})
    step = {
        "op": "btc-sign-psbt",
        "name": VECTOR_SIGN_STEP,
        "coin": v["coin"],
        "psbt": v["psbt"]["transaction"],
        "registrations": v.get("registrations", []),
        "force_script_config": opts.get("force_script_config"),
        "format_unit": opts.get("format_unit", "default"),
    }
    return {
        "id": f"btc-vector/{v['id']}",
        "kind": "btc-vector",
        "scenario": {"name": f"btc-vector-{v['id']}", "description": v["description"], "steps": [step]},
        "expect": [{"min_version": e.get("min_version"), "max_version_exclusive": e.get("max_version_exclusive"),
                    "outcome": e["outcome"], "screens": e["screens"]} for e in v["expectations"]],
    }


def load_units(firmware_dir, scenarios_dir, ref=None):
    """Scenario units from scenarios_dir, the EVM matrix, then one unit per BTC vector of the
    firmware (at `ref` if given, else the working tree)."""
    units = []
    for p in sorted(Path(scenarios_dir).glob("*.json")):
        sc = json.loads(p.read_text())
        name = sc.get("name") or p.stem
        units.append({
            "id": f"scenario/{name}",
            "kind": "scenario",
            "scenario": {"name": name, "description": sc.get("description", ""), "steps": sc["steps"]},
            "expect": sc.get("expect"),
            "path": str(p),
        })
    units += eth_matrix.units(Path(scenarios_dir) / "eth-matrix")
    vectors = read_vectors(firmware_dir, ref)
    for v in (vectors or {}).get("vectors", []):
        # Same skip as bitbox-api-rs: btc_sign_psbt can't pass per-output options or payment
        # requests (silent payments, swaps), so those vectors can't be signed through it.
        opts = v["psbt"].get("options", {})
        if opts.get("outputs") or opts.get("payment_requests"):
            continue
        units.append(vector_unit(v))
    return units


if __name__ == "__main__":
    args = sys.argv[1:]
    want = None
    if "--scenario" in args:
        i = args.index("--scenario")
        want = args[i + 1]
        del args[i:i + 2]
    e2e_dir = Path(__file__).resolve().parent
    all_units = load_units(Path(args[0]), e2e_dir / "scenarios", args[1] if len(args) > 1 else None)
    if want is None:
        for u in all_units:
            print(u["id"])
    else:
        u = next((u for u in all_units if u["id"] == want), None)
        if u is None:
            sys.exit(f"unknown unit {want}")
        print(json.dumps(u["scenario"], indent=2))
