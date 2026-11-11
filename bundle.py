"""Units, baseline bundles and the parallel unit runner shared by `e2e.py baseline` and `e2e.py pr`.

Bundle layout ($BITBOX_E2E_CACHE/baselines/<sha>/, see DESIGN.md):
  manifest.json, units/<slug>/{scenario.json,meta.json,headless/,graphical/}, apis/, coverage/
"""

import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from queue import Queue

import units
from units import slug  # noqa: F401  (re-exported for e2e.py)

E2E_DIR = Path(__file__).resolve().parent
SCENARIOS = E2E_DIR / "scenarios"
CACHE = Path(os.environ.get("BITBOX_E2E_CACHE", Path.home() / ".cache" / "bitbox-e2e"))
FW = Path(os.environ.get("BITBOX_FIRMWARE", E2E_DIR.parent / "bitbox02-firmware"))
FW_VECTORS = units.VECTORS_PATH


def log(msg):
    print(f"[e2e] {msg}", file=sys.stderr, flush=True)


def git(*args, cwd=FW):
    return subprocess.run(["git", "-C", str(cwd), *args], stdout=subprocess.PIPE, text=True,
                          check=True).stdout.strip()


def resolve(ref):
    return git("rev-parse", "HEAD") if ref == "WORKTREE" else git("rev-parse", "--verify", f"{ref}^{{commit}}")


def bundle_dir(sha):
    return CACHE / "baselines" / sha


def harness_sha():
    dirty = git("status", "--porcelain", "--", ".", cwd=E2E_DIR)
    return git("rev-parse", "HEAD", cwd=E2E_DIR) + ("+dirty" if dirty else "")


def read_manifest(root):
    p = Path(root) / "manifest.json"
    return json.loads(p.read_text()) if p.exists() else None


def write_manifest(root, m):
    (Path(root) / "manifest.json").write_text(json.dumps(m, indent=2))


def firmware_dir(ref):
    """A directory units.load_units can read the firmware's files from. WORKTREE is the checkout
    itself; for commits only the files units need are materialized, no full checkout."""
    if ref == "WORKTREE":
        return FW
    sha = resolve(ref)
    d = CACHE / "src" / f"fw-files-{sha}"
    dst = d / FW_VECTORS
    if not dst.exists():
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(git("show", f"{sha}:{FW_VECTORS}") + "\n")
    return d


def load_units(fw_dir):
    return units.load_units(fw_dir, SCENARIOS)


def run_units(sim_dir, unit_list, root, view, jobs):
    """Runs each unit in its own network namespace (`isolated.sh e2e.py device`), `jobs` at a time,
    writing <root>/units/<slug>/. view is "headless" or "graphical". Returns {id: ok}.

    Graphical slots use consecutive X displays starting at $E2E_DISPLAY."""
    base_disp = int(os.environ.get("E2E_DISPLAY", ":82").lstrip(":"))
    displays = Queue()
    for i in range(jobs):
        displays.put(f":{base_disp + i}")

    def one(u):
        d = Path(root) / "units" / slug(u["id"])
        d.mkdir(parents=True, exist_ok=True)
        (d / "scenario.json").write_text(json.dumps(u["scenario"], indent=2))
        disp = displays.get()
        try:
            skip = "--no-graphical" if view == "headless" else "--no-headless"
            cmd = ["nice", str(E2E_DIR / "isolated.sh"), sys.executable, str(E2E_DIR / "e2e.py"), "device",
                   "--scenario", str(d / "scenario.json"), "--sim-dir", str(sim_dir), "--out", str(d),
                   "--label", u["id"], skip]
            with open(d / f"{view}.log", "w") as f:
                subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT, timeout=900,
                               env=dict(os.environ, E2E_DISPLAY=disp))
        except subprocess.TimeoutExpired:
            pass
        finally:
            displays.put(disp)
        meta = d / "meta.json"
        return json.loads(meta.read_text()).get(f"{view}_ok") if meta.exists() else False

    t0 = time.time()
    with ThreadPoolExecutor(jobs) as ex:
        res = dict(zip([u["id"] for u in unit_list], ex.map(one, unit_list)))
    log(f"{view}: {sum(bool(v) for v in res.values())}/{len(res)} units ok in {time.time() - t0:.0f}s")
    return res
