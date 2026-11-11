"""Units: one scenario run each, from scenarios/*.json and the firmware's BTC test vectors.

  units.py <firmware_dir> [ref]     list unit ids
"""

import json
import re
import subprocess
import sys
from pathlib import Path

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
    """Scenario units from scenarios_dir, then one unit per BTC vector of the firmware (at `ref`
    if given, else the working tree)."""
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
    e2e_dir = Path(__file__).resolve().parent
    for u in load_units(Path(sys.argv[1]), e2e_dir / "scenarios", sys.argv[2] if len(sys.argv) > 2 else None):
        print(u["id"])
