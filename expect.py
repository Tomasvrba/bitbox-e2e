"""Check a unit's headless screens against its version-ranged expectations, and promote screens.

  expect.py check <unit-id> <screens.json> <version.txt> [firmware_dir]
  expect.py promote <scenario.json> <screens.json> <version.txt>

`screens` is the screens.json list run_headless writes ({step, kind, fields}). Vector
expectations use the firmware's vector format ({type, ...}, as parsed by bitbox-api-rs
tests/util parse_simulator_screens); both are normalized to that form before comparing.
"""

import difflib
import json
import re
import sys
from pathlib import Path

import units

SETUP_STEPS = {"boot", "connect", "pairing", "seed"}

# Simulator screen kind -> (vector screen type, field order).
KINDS = {
    "CONFIRM": ("confirm", ["TITLE", "BODY"]),
    "CONFIRM TRANSACTION ADDRESS": ("transaction_address", ["AMOUNT", "ADDRESS"]),
    "CONFIRM TRANSACTION FEE": ("transaction_fee", ["AMOUNT", "FEE"]),
    "CONFIRM SWAP": ("swap", ["TITLE", "FROM", "TO"]),
}


def parse_version(s):
    """'bitbox02-multi-v9.28.0-simulator1.0.0-linux-amd64' or '9.28.0' -> (9, 28, 0)."""
    m = re.search(r"(?:^|-v)(\d+)\.(\d+)\.(\d+)", s.strip())
    if not m:
        raise ValueError(f"no firmware version in {s!r}")
    return tuple(int(x) for x in m.groups())


def normalize(screen):
    """screens.json entry -> vector-format dict. ScreenParser splits on any `UPPER: ` line, so
    rebuild the raw block and re-split on the known field names like api-rs does."""
    lines = "\n".join(f"{k}: {v}" for k, v in screen["fields"].items()).split("\n")
    kind = screen["kind"]
    if kind == "STATUS":
        # Status prints only TITLE; api-rs treats its first line as title, the rest as body.
        return {"type": "status", "title": lines[0].removeprefix("TITLE: "), "body": "\n".join(lines[1:])}
    if kind not in KINDS:
        return {"type": kind.lower().replace(" ", "_"), **{k.lower(): v for k, v in screen["fields"].items()}}
    typ, names = KINDS[kind]
    out, i = {"type": typ}, 0
    for n, name in enumerate(names):
        if i >= len(lines) or not lines[i].startswith(f"{name}: "):
            return {"type": typ, "malformed": "\n".join(lines)}
        val, i = [lines[i][len(name) + 2:]], i + 1
        nxt = f"{names[n + 1]}: " if n + 1 < len(names) else None
        while i < len(lines) and not (nxt and lines[i].startswith(nxt)):
            val.append(lines[i])
            i += 1
        out[name.lower()] = "\n".join(val)
    return out


def render(screens):
    lines = []
    for s in screens:
        head = f"[{s['step']}] {s['type']}" if "step" in s else s["type"]
        lines.append(head)
        lines += [f"  {k}: {json.dumps(v, ensure_ascii=False)}" for k, v in s.items()
                  if k not in ("type", "step", "longtouch")]
    return lines


def observed(unit, screens):
    if unit["kind"] == "btc-vector":
        sign = unit["scenario"]["steps"][-1]["name"]
        return [normalize(s) for s in screens if s["step"] == sign]
    return [{"step": s["step"], **normalize(s)} for s in screens if s["step"] not in SETUP_STEPS]


def expected(unit, exp):
    if unit["kind"] == "btc-vector":
        return [{k: v for k, v in s.items() if k != "longtouch"} for s in exp["screens"]]
    return observed(unit, exp["screens"])


def in_range(exp, v):
    lo, hi = exp.get("min_version"), exp.get("max_version_exclusive")
    return (lo is None or v >= parse_version(lo)) and (hi is None or v < parse_version(hi))


def check(unit, screens, simulator_version):
    v = parse_version(simulator_version)
    exp = next((e for e in unit.get("expect") or [] if in_range(e, v)), None)
    if exp is None:
        return {"status": "no-expectation", "diff": ""}
    want, got = render(expected(unit, exp)), render(observed(unit, screens))
    if want == got:
        return {"status": "pass", "diff": ""}
    rng = f"[{exp.get('min_version')}, {exp.get('max_version_exclusive')})"
    diff = difflib.unified_diff(want, got, f"expected {rng}", f"observed {'.'.join(map(str, v))}", lineterm="")
    return {"status": "fail", "diff": "\n".join(diff)}


def promote(scenario_path, screens, simulator_version):
    """Store `screens` as the expectation from this version on. An open range starting earlier is
    closed here; an entry starting at exactly this version is replaced."""
    p = Path(scenario_path)
    # Generated units (eth-matrix) get their expectation file on first promote.
    sc = json.loads(p.read_text()) if p.exists() else {}
    p.parent.mkdir(parents=True, exist_ok=True)
    v = parse_version(simulator_version)
    vs = ".".join(map(str, v))
    entries = [e for e in sc.get("expect") or [] if e.get("min_version") is None or parse_version(e["min_version"]) != v]
    later = [e["min_version"] for e in entries if e.get("min_version") and parse_version(e["min_version"]) > v]
    for e in entries:
        lo, hi = e.get("min_version"), e.get("max_version_exclusive")
        if (lo is None or parse_version(lo) < v) and (hi is None or parse_version(hi) > v):
            e["max_version_exclusive"] = vs
    entries.append({"min_version": vs, "max_version_exclusive": min(later, key=parse_version) if later else None,
                    "screens": [s for s in screens if s["step"] not in SETUP_STEPS]})
    sc["expect"] = sorted(entries, key=lambda e: parse_version(e["min_version"]) if e.get("min_version") else (0,))
    p.write_text(json.dumps(sc, indent=2) + "\n")


def main():
    e2e_dir = Path(__file__).resolve().parent
    cmd, args = sys.argv[1], sys.argv[2:]
    screens = json.loads(Path(args[1]).read_text())
    version = Path(args[2]).read_text()
    if cmd == "check":
        fw = Path(args[3]) if len(args) > 3 else e2e_dir.parent / "bitbox02-firmware"
        unit = next((u for u in units.load_units(fw, e2e_dir / "scenarios") if u["id"] == args[0]), None)
        if unit is None:
            sys.exit(f"unknown unit {args[0]}")
        res = check(unit, screens, version)
        print(res["status"])
        if res["diff"]:
            print(res["diff"])
        sys.exit(res["status"] == "fail")
    elif cmd == "promote":
        promote(args[0], screens, version)
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
