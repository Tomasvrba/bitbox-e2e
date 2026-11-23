#!/usr/bin/env python3
"""Lane B: run the host API simulator suites (api-go, api-rs, api-ts) against one simulator.

  apis.py <sim-dir> <firmware-vectors.json> <out-dir>

Each suite runs twice, from scratch clones of the sibling checkout's HEAD (or a pinned commit):
  committed  the repo's own copy of the BTC transaction vectors (drift vs. the firmware)
  firmware   the firmware ref's vectors copied in (regression of the firmware itself); skipped for
             refs without the vectors file

The suites connect to the simulator on 15423. Deps are built first, outside any network namespace;
the suites then run inside isolated.sh, all six at once, or one at a time where namespaces are
unavailable.
"""

import contextlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import bundle
from bundle import CACHE, E2E_DIR

MODES = ("committed", "firmware")
# Suite name = bundle.REPOS key; the repo's copy of the firmware's BTC transaction vectors.
SUITES = {
    "api-go": "api/firmware/testdata/btc-transaction-test-vectors.json",
    "api-rs": "tests/data/btc-transaction-test-vectors.json",
    "api-ts": "test/data/btc-transaction-test-vectors.json",
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


def scratch_clone(suite, mode, sha=None):
    """Persistent per-(suite, mode) clone at sha, default the sibling's HEAD, so build dirs and
    node_modules survive between runs while the two modes can build and run concurrently."""
    src = bundle.REPOS[suite].dir
    sha = sha or bundle.git("rev-parse", "HEAD", cwd=src)
    dst = CACHE / "src" / f"{src.name}-{mode}"
    if not (dst / ".git").exists():
        # --shared: the clone sees every object of the sibling, including fetched fork commits.
        sh(["git", "clone", "-q", "--shared", "--no-checkout", str(src), str(dst)])
    # -f also restores the committed vectors file a previous firmware-mode run overwrote.
    sh(["git", "-C", str(dst), "checkout", "-q", "-f", "--detach", sha])
    return dst, sha, dst / SUITES[suite]


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
        # The gate only counts head failures of tests that passed on base.
        "passing": sorted(k for k, v in st.items() if v == "pass"),
    }


def suite_error(code, res):
    """Why a suite run says nothing about the firmware, or None. `res` is parse()'s output."""
    # A non-zero exit with no failing test parsed means the suite itself broke (panic, compile, setup).
    if code and not res["failing"]:
        return f"exit {code}"
    # Exit 0 with nothing run is a vacuous pass: a test filter that matches nothing, a skipped
    # simulator setup, or output this parser no longer understands.
    if not res["passed"] and not res["failing"]:
        return "no tests ran"
    return None


def run_one(suite, mode, sim_dir, fw_vectors, out, run_lock, sha):
    t0 = time.time()
    clone, sha, vectors = scratch_clone(suite, mode, sha)
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
        return dict(res, error="build failed", passed=0, failed=0, skipped=0, failing=[], passing=[])
    t1 = time.time()
    with run_lock or contextlib.nullcontext(), open(logf, "w") as f:
        t2 = time.time()  # waiting for the lock is neither build nor run time
        if run_lock:
            bundle.wait_port_free(bundle.SIM_PORT)
        isolated = [] if run_lock else [str(E2E_DIR / "isolated.sh")]
        p = subprocess.run(["nice", *isolated, *run_cmd(suite, out_json)], cwd=clone, env=env,
                           stdout=f, stderr=subprocess.STDOUT, timeout=3600)
    text = logf.read_text(errors="replace")
    res.update(parse(suite, text, out_json), exit=p.returncode,
               build_s=round(t1 - t0), run_s=round(time.time() - t2))
    if err := suite_error(p.returncode, res):
        res["error"] = err
    log(f"{suite} {mode}: {res['passed']} passed, {res['failed']} failed ({res['run_s']}s)")
    return res


def run(sim_dir, fw_vectors, out, suites=tuple(SUITES), revs=None):
    """Returns {suite: {mode: result}} and writes it to <out>/results.json. revs: {suite: sha} to
    run instead of the sibling's HEAD."""
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    if "api-ts" in suites:
        bundle.use_node()
    modes = MODES if Path(fw_vectors).exists() else ("committed",)
    # Without namespaces every suite's simulator binds 15423 on the host: builds stay parallel, runs take turns.
    run_lock = None if bundle.namespaces() else threading.Lock()
    jobs = [(s, m) for s in suites for m in modes]
    with ThreadPoolExecutor(len(jobs)) as ex:
        futs = {j: ex.submit(run_one, *j, Path(sim_dir), Path(fw_vectors), out, run_lock, (revs or {}).get(j[0]))
                for j in jobs}
    results = {}
    for (s, m), f in futs.items():
        try:
            results.setdefault(s, {})[m] = f.result()
        except Exception as e:  # one broken suite must not hide the others
            results.setdefault(s, {})[m] = {"error": repr(e), "passed": 0, "failed": 0, "skipped": 0, "failing": [],
                                            "passing": []}
    results = {"simulator": (Path(sim_dir) / "version.txt").read_text().strip(), "suites": results}
    (out / "results.json").write_text(json.dumps(results, indent=2))
    return results


if __name__ == "__main__":
    if len(sys.argv) != 4:
        sys.exit(__doc__)
    r = run(*sys.argv[1:])
    print(json.dumps({s: {m: (v["passed"], v["failed"]) for m, v in ms.items()} for s, ms in r["suites"].items()}))
