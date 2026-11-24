"""Units: one scenario run each, from scenarios/*.json, the generated EVM matrix (eth_matrix.py),
and the firmware's BTC test vectors.
"""

import json
import re
import subprocess
from pathlib import Path

from bitbox_e2e import eth_matrix
from bitbox_e2e.config import FW_VECTORS, SCENARIOS, slug  # noqa: F401

VECTORS_PATH = FW_VECTORS
VECTOR_SIGN_STEP = "sign"


def parse_version(s: str) -> tuple[int, int, int]:
    """'bitbox02-multi-v9.28.0-simulator1.0.0-linux-amd64' or '9.28.0' -> (9, 28, 0)."""
    m = re.search(r"(?:^|-v)(\d+)\.(\d+)\.(\d+)", s.strip())
    if not m:
        raise ValueError(f"no firmware version in {s!r}")
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def _in_range(exp: dict, v: tuple[int, int, int]) -> bool:
    lo, hi = exp.get("min_version"), exp.get("max_version_exclusive")
    return (lo is None or v >= parse_version(lo)) and (hi is None or v < parse_version(hi))


def vector_outcome(unit: dict, simulator_version: str) -> str | None:
    """What a BTC vector expects of this version: 'success', or a rejection ('invalid_input',
    'unsupported'). None for units without expectations."""
    exps = unit.get("outcomes") or unit.get("expect") or []
    if not exps:
        return None
    v = parse_version(simulator_version)
    return next((e.get("outcome") for e in exps if _in_range(e, v)), None)


def read_vectors(firmware_dir: Path, ref: str | None = None) -> dict | None:
    if ref is None:
        p = Path(firmware_dir) / FW_VECTORS
        return json.loads(p.read_text()) if p.exists() else None
    res = subprocess.run(
        ["git", "-C", str(firmware_dir), "show", f"{ref}:{FW_VECTORS}"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    return json.loads(res.stdout) if res.returncode == 0 else None


def vector_unit(v: dict) -> dict:
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
        "outcomes": [
            {
                "min_version": e.get("min_version"),
                "max_version_exclusive": e.get("max_version_exclusive"),
                "outcome": e["outcome"],
            }
            for e in v["expectations"]
        ],
    }


def load_units(firmware_dir: Path, scenarios_dir: Path = SCENARIOS, ref: str | None = None) -> list[dict]:
    """Scenario units from scenarios_dir, the EVM matrix, then one unit per BTC vector of the
    firmware (at `ref` if given, else the working tree)."""
    units = []
    for p in sorted(Path(scenarios_dir).glob("*.json")):
        sc = json.loads(p.read_text())
        name = sc.get("name") or p.stem
        units.append(
            {
                "id": f"scenario/{name}",
                "kind": "scenario",
                "scenario": {"name": name, "description": sc.get("description", ""), "steps": sc["steps"]},
            }
        )
    units += eth_matrix.units()
    vectors = read_vectors(firmware_dir, ref)
    for v in (vectors or {}).get("vectors", []):
        # Same skip as bitbox-api-rs: btc_sign_psbt can't pass per-output options or payment
        # requests (silent payments, swaps), so those vectors can't be signed through it.
        opts = v["psbt"].get("options", {})
        if opts.get("outputs") or opts.get("payment_requests"):
            continue
        units.append(vector_unit(v))
    return units
