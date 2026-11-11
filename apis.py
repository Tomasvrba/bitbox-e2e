#!/usr/bin/env python3
"""Lane B: run the host API simulator suites (api-go, api-rs, api-ts) against one simulator.

  apis.py <sim-dir> <firmware-vectors.json> <out-dir>

Each suite runs twice, from scratch clones of the sibling checkout's HEAD:
  committed  the repo's own copy of the BTC transaction vectors (drift vs. the firmware)
  firmware   the firmware ref's vectors copied in (regression of the firmware itself)

Deps are built outside the network namespace; the suites themselves run inside isolated.sh, all
six at once (each namespace has its own port 15423).
"""

import json
import os
import re
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

E2E_DIR = Path(__file__).resolve().parent
WORKSPACE = Path(os.environ.get("BITBOX_WORKSPACE", E2E_DIR.parent))
CACHE = Path(os.environ.get("BITBOX_E2E_CACHE", Path.home() / ".cache" / "bitbox-e2e"))
FW_VECTORS = "src/rust/bitbox-test-vectors/testdata/btc-transaction-test-vectors.json"
MODES = ("committed", "firmware")
SUITES = {
    "api-go": ("bitbox02-api-go", "api/firmware/testdata/btc-transaction-test-vectors.json"),
    "api-rs": ("bitbox-api-rs", "tests/data/btc-transaction-test-vectors.json"),
    "api-ts": ("bitbox-api-ts", "test/data/btc-transaction-test-vectors.json"),
}
RS_FEATURES = "--features=simulator,tokio"


def log(msg):
    print(f"[apis] {msg}", file=sys.stderr, flush=True)


def sh(cmd, **kw):
    return subprocess.run(cmd, check=True, **kw)


def release_binary(sim_dir):
    """api-ts parses the firmware version out of the file name; build-sim.sh links the release name."""
    links = sorted(Path(sim_dir).glob("bitbox02-*-simulator*"))
    return links[0] if links else Path(sim_dir) / "simulator"


def scratch_clone(suite, mode):
    """Persistent per-(suite, mode) clone at the sibling's HEAD, so build dirs and node_modules
    survive between runs while the two modes can build and run concurrently."""
    repo, vectors = SUITES[suite]
    src = WORKSPACE / repo
    sha = subprocess.run(["git", "-C", str(src), "rev-parse", "HEAD"], stdout=subprocess.PIPE, text=True,
                         check=True).stdout.strip()
    dst = CACHE / "src" / f"{repo}-{mode}"
    if not (dst / ".git").exists():
        sh(["git", "clone", "-q", "--shared", "--no-checkout", str(src), str(dst)])
    sh(["git", "-C", str(dst), "fetch", "-q", str(src), sha])
    # -f also restores the committed vectors file a previous firmware-mode run overwrote.
    sh(["git", "-C", str(dst), "checkout", "-q", "-f", "--detach", sha])
    return dst, sha, dst / vectors


def build(suite, clone, env):
    """Everything that may touch the network or compile, so the isolated run only executes."""
    if suite == "api-go":
        sh(["nice", "go", "test", "-c", "-o", "firmware.test", "./api/firmware/"], cwd=clone, env=env,
           stdout=sys.stderr)
    elif suite == "api-rs":
        # tests/util reads SIMULATOR with option_env!, i.e. at compile time; cargo tracks it, so
        # the isolated `cargo test` with the same env is a no-op build.
        sh(["nice", "cargo", "test", "-q", RS_FEATURES, "--no-run"], cwd=clone, env=env, stdout=sys.stderr)
    else:
        lock = (clone / "package-lock.json").read_bytes()
        stamp = clone / "node_modules" / ".e2e-lock"
        if not stamp.exists() or stamp.read_bytes() != lock:
            sh(["nice", "npm", "ci", "--ignore-scripts", "--no-audit", "--no-fund"], cwd=clone, env=env, stdout=sys.stderr)
            stamp.write_bytes(lock)


def run_cmd(suite, out_json):
    if suite == "api-go":
        # Same as `go test -count=1 -run TestSimulator ./api/firmware/`, minus the build.
        return ["sh", "-c", "cd api/firmware && exec ../../firmware.test -test.count=1 -test.v "
                            "-test.run TestSimulator"]
    if suite == "api-rs":
        # --no-fail-fast: by default cargo stops at the first failing test binary.
        return ["cargo", "test", "--offline", "--no-fail-fast", RS_FEATURES, "--", "--test-threads", "1"]
    return ["npx", "vitest", "run", "--project", "simulator", "--reporter=default", "--reporter=json",
            f"--outputFile.json={out_json}"]


def leaves(names):
    """Drop parent tests: Go reports TestX and TestX/sub, we count only what actually ran."""
    return sorted(n for n in names if not any(o.startswith(n + "/") for o in names))


def parse(suite, text, out_json):
    st = {}
    if suite == "api-go":
        for m in re.finditer(r"^\s*--- (PASS|FAIL|SKIP): (\S+)", text, re.M):
            st[m.group(2)] = m.group(1).lower()
        keep = set(leaves(list(st)))
        st = {k: v for k, v in st.items() if k in keep}
    elif suite == "api-rs":
        for m in re.finditer(r"^test (\S+) \.\.\. (ok|FAILED|ignored)", text, re.M):
            st[m.group(1)] = {"ok": "pass", "FAILED": "fail", "ignored": "skip"}[m.group(2)]
    elif out_json.exists():
        for f in json.loads(out_json.read_text())["testResults"]:
            for a in f["assertionResults"]:
                st[a["fullName"]] = {"passed": "pass", "failed": "fail"}.get(a["status"], "skip")
    return {
        "passed": sum(v == "pass" for v in st.values()),
        "failed": sum(v == "fail" for v in st.values()),
        "skipped": sum(v == "skip" for v in st.values()),
        "failing": sorted(k for k, v in st.items() if v == "fail"),
    }


def run_one(suite, mode, sim_dir, fw_vectors, out):
    t0 = time.time()
    clone, sha, vectors = scratch_clone(suite, mode)
    if mode == "firmware":
        shutil.copy(fw_vectors, vectors)
    env = dict(os.environ, SIMULATOR=str(release_binary(sim_dir)))
    logf = out / f"{suite}-{mode}.log"
    out_json = out / f"{suite}-{mode}.json"
    out_json.unlink(missing_ok=True)
    res = {"repo_sha": sha, "log": logf.name}
    try:
        build(suite, clone, env)
    except subprocess.CalledProcessError as e:
        logf.write_text(f"build failed: {e}\n")
        return dict(res, error="build failed", passed=0, failed=0, skipped=0, failing=[])
    t1 = time.time()
    with open(logf, "w") as f:
        p = subprocess.run(["nice", str(E2E_DIR / "isolated.sh"), *run_cmd(suite, out_json)], cwd=clone, env=env,
                           stdout=f, stderr=subprocess.STDOUT, timeout=3600)
    text = logf.read_text(errors="replace")
    res.update(parse(suite, text, out_json), exit=p.returncode,
               build_s=round(t1 - t0), run_s=round(time.time() - t1))
    # A non-zero exit with no failing test parsed means the suite itself broke (panic, compile, setup).
    if p.returncode and not res["failing"]:
        res["error"] = f"exit {p.returncode}"
    log(f"{suite} {mode}: {res['passed']} passed, {res['failed']} failed ({res['run_s']}s)")
    return res


def run(sim_dir, fw_vectors, out, suites=tuple(SUITES)):
    """Returns {suite: {mode: result}} and writes it to <out>/results.json."""
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    jobs = [(s, m) for s in suites for m in MODES]
    with ThreadPoolExecutor(len(jobs)) as ex:
        futs = {j: ex.submit(run_one, *j, Path(sim_dir), Path(fw_vectors), out) for j in jobs}
    results = {}
    for (s, m), f in futs.items():
        try:
            results.setdefault(s, {})[m] = f.result()
        except Exception as e:  # one broken suite must not hide the others
            results.setdefault(s, {})[m] = {"error": repr(e), "passed": 0, "failed": 0, "skipped": 0, "failing": []}
    results = {"simulator": (Path(sim_dir) / "version.txt").read_text().strip(), "suites": results}
    (out / "results.json").write_text(json.dumps(results, indent=2))
    return results


if __name__ == "__main__":
    if len(sys.argv) != 4:
        sys.exit(__doc__)
    r = run(*sys.argv[1:])
    print(json.dumps({s: {m: (v["passed"], v["failed"]) for m, v in ms.items()} for s, ms in r["suites"].items()}))
