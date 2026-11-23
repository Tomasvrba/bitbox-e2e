"""Shared by every harness script: where things live, ports, the network namespace probe, node 26.
Also units, baseline bundles and the parallel unit runner of `e2e.py baseline` and `e2e.py pr`.

Bundle layout ($BITBOX_E2E_CACHE/baselines/<sha>/, see DESIGN.md):
  manifest.json, units/<slug>/{scenario.json,meta.json,headless/,graphical/}, apis/, coverage/
"""

import functools
import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import NamedTuple

import units
from units import slug  # noqa: F401  (re-exported for e2e.py)

# Same defaults as env.sh, for runs that did not source it.
E2E_DIR = Path(__file__).resolve().parent
WORKSPACE = Path(os.environ.get("BITBOX_WORKSPACE", E2E_DIR.parent))
CACHE = Path(os.environ.get("BITBOX_E2E_CACHE", Path.home() / ".cache" / "bitbox-e2e"))
TOOLS = Path(os.environ.get("BITBOX_E2E_TOOLS", Path(os.environ.get(
    "BITBOX_E2E_HOME", Path.home() / ".local" / "share" / "bitbox-e2e")) / "tools"))
FW = Path(os.environ.get("BITBOX_FIRMWARE", WORKSPACE / "bitbox02-firmware"))
SCENARIOS = E2E_DIR / "scenarios"


class Repo(NamedTuple):
    dir: Path
    slug: str  # on GitHub
    branch: str  # default branch


# The repos a change under test can be in (`e2e.py pr --repo`), checked out next to e2e/.
REPOS = {
    "firmware": Repo(FW, "BitBoxSwiss/bitbox02-firmware", "master"),
    "app": Repo(Path(os.environ.get("BITBOX_WALLET_APP", WORKSPACE / "bitbox-wallet-app")),
                "BitBoxSwiss/bitbox-wallet-app", "master"),
    "api-rs": Repo(WORKSPACE / "bitbox-api-rs", "BitBoxSwiss/bitbox-api-rs", "master"),
    "api-go": Repo(WORKSPACE / "bitbox02-api-go", "BitBoxSwiss/bitbox02-api-go", "master"),
    "api-ts": Repo(WORKSPACE / "bitbox-api-ts", "BitBoxSwiss/bitbox-api-ts", "master"),
    "rabby": Repo(Path(os.environ.get("BITBOX_RABBY", WORKSPACE / "Rabby")), "RabbyHub/Rabby", "develop"),
}
FW_VECTORS = units.VECTORS_PATH
# Where the API simulator suites, the app's simulator mode and graphical simulators without --port
# connect. Everything else gets a free port.
SIM_PORT = 15423
# A graphical simulator busy-loops a core and misses host polls under load (README); headless units
# would take more, but one -j serves both.
JOBS = max(1, len(os.sched_getaffinity(0)) // 2)


def log(msg):
    print(f"[e2e] {msg}", file=sys.stderr, flush=True)


def git(*args, cwd=None):
    return subprocess.run(["git", "-C", str(cwd or FW), *args], stdout=subprocess.PIPE, text=True,
                          check=True).stdout.strip()


def resolve(ref, repo="firmware"):
    """Commit sha of ref in one of REPOS. WORKTREE is the firmware checkout's HEAD (build-sim.sh
    builds its files as they are)."""
    cwd = REPOS[repo].dir
    if ref == "WORKTREE":
        return git("rev-parse", "HEAD", cwd=cwd)
    return git("rev-parse", "--verify", f"{ref}^{{commit}}", cwd=cwd)



def read_text(p):
    return p.read_text().strip() if p.exists() else ""


def sim_version(sim_dir):
    """The simulator's release name; expectations are picked by the version in it."""
    v = read_text(Path(sim_dir) / "version.txt")
    if not v:
        raise RuntimeError(f"{sim_dir}/version.txt is missing or empty; rebuild it with build-sim.sh")
    return v


def build_sim(ref):
    """Simulators for a firmware ref (build-sim.sh, cached by commit); returns their directory."""
    log(f"building simulators for {ref}")
    res = subprocess.run([str(E2E_DIR / "build-sim.sh"), ref], stdout=subprocess.PIPE, text=True, check=True)
    return Path(res.stdout.strip().splitlines()[-1])

def bundle_dir(sha, revs=None):
    """Bundle of a firmware commit. revs pins other repos ({"api-rs": sha, ...}); those bundles are
    kept apart from the default one, which uses whatever the sibling checkouts had."""
    if not revs:
        return CACHE / "baselines" / sha
    key = hashlib.sha256(json.dumps(revs, sort_keys=True).encode()).hexdigest()[:12]
    return CACHE / "baselines" / f"{sha}-{key}"


def checkout_rev(path):
    """HEAD of the git checkout at path, "+dirty" with uncommitted changes under it."""
    dirty = git("status", "--porcelain", "--", ".", cwd=path)
    return git("rev-parse", "HEAD", cwd=path) + ("+dirty" if dirty else "")


def harness_sha():
    return checkout_rev(E2E_DIR)


def upstream_remote(repo, github_slug):
    """The remote of repo pointing at github.com/<github_slug>, else "origin". Remote names are
    per clone: a CI checkout only has origin, a developer's origin is often their fork."""
    url_re = re.compile(rf"github\.com[:/]{re.escape(github_slug)}(\.git)?/?$", re.I)
    for line in git("remote", "-v", cwd=repo).splitlines():
        name, url = line.split()[:2]
        if url_re.search(url):
            return name
    return "origin"


def default_ref(repo):
    """The default branch of the upstream repo, e.g. bitbox/master in a developer's firmware clone."""
    r = REPOS[repo]
    return f"{upstream_remote(r.dir, r.slug)}/{r.branch}"


def default_firmware_ref():
    return default_ref("firmware")


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
    d.mkdir(parents=True, exist_ok=True)
    if not dst.exists():
        # Refs before the vectors file (firmware 3bdaf583b) have no btc-vector units.
        r = subprocess.run(["git", "-C", str(FW), "show", f"{sha}:{FW_VECTORS}"], stdout=subprocess.PIPE,
                           stderr=subprocess.DEVNULL, text=True)
        if r.returncode == 0:
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_text(r.stdout + "\n")
    return d


def has_vectors(fw_dir):
    return (Path(fw_dir) / FW_VECTORS).exists()


def load_units(fw_dir):
    return units.load_units(fw_dir, SCENARIOS)


# --- running simulators side by side ---------------------------------------------------------------

def free_port():
    """A port a simulator can bind right now. Bound like the headless simulator binds (0.0.0.0, no
    SO_REUSEADDR), so the kernel does not hand out a port that is in TIME_WAIT."""
    with socket.socket() as s:
        s.bind(("0.0.0.0", 0))
        return s.getsockname()[1]


def wait_port_free(port, timeout_s=70, reuseaddr=False):
    """Waits until a simulator could bind port, tested the way it binds. The headless simulator (C)
    binds without SO_REUSEADDR, so it has to wait out TIME_WAIT (up to 60s on Linux) after the last
    one. simulator-graphical (Rust std TcpListener) sets it: reuseaddr=True, or every run on the
    fixed port would idle for a minute first."""
    deadline = time.time() + timeout_s
    while True:
        with socket.socket() as s:
            if reuseaddr:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("0.0.0.0", port))
                return
            except OSError:
                pass
        if time.time() >= deadline:
            raise RuntimeError(f"port {port} still busy; is another simulator running?")
        time.sleep(0.2)


@functools.cache
def namespaces():
    """Whether isolated.sh works here. Most CI containers block unprivileged user namespaces."""
    r = subprocess.run([str(E2E_DIR / "isolated.sh"), "true"], stdout=subprocess.DEVNULL,
                       stderr=subprocess.PIPE, text=True)
    if r.returncode:
        log("no network namespaces here, fixed-port runs go one at a time: "
            + " ".join(r.stderr.split()[:30]))
    return r.returncode == 0


@functools.cache
def takes_port(sim_dir):
    """Whether this simulator-graphical has --port. Firmware before a540c993b always binds 15423."""
    try:
        r = subprocess.run([str(Path(sim_dir) / "simulator-graphical"), "--help"], stdout=subprocess.PIPE,
                           stderr=subprocess.DEVNULL, text=True, timeout=30)
    except OSError:
        return False
    return "--port" in r.stdout


# How graphical units run on a simulator, see graphical_mode().
GRAPHICAL_MODES = {
    "port": "parallel, own --port each",
    "netns": "parallel, one network namespace each (simulator has no --port)",
    "serial": f"one at a time on {SIM_PORT} (simulator has no --port, no network namespaces here)",
}


def graphical_mode(sim_dir):
    if takes_port(sim_dir):
        return "port"
    return "netns" if namespaces() else "serial"


def use_node():
    """Puts node 26 (env.sh's BITBOX_NODE_BIN) first on PATH for every child process. The app needs
    >=26.12.1; another node fails late and confusingly, so refuse to start without it."""
    node_bin = os.environ.get("BITBOX_NODE_BIN")
    if not node_bin:
        sys.exit("BITBOX_NODE_BIN is unset, so there is no node 26: run e2e/setup.sh (it installs one), "
                 "then source e2e/env.sh")
    if os.environ.get("PATH", "").split(os.pathsep)[0] != node_bin:
        os.environ["PATH"] = node_bin + os.pathsep + os.environ.get("PATH", "")


def run_units(sim_dir, unit_list, root, view, jobs, driver=None):
    """Runs `e2e.py device` per unit, `jobs` at a time, writing <root>/units/<slug>/. view is
    "headless" or "graphical"; driver is a driver binary (runs.driver_for), default E2E_DRIVER.
    Returns {id: ok}.

    Headless units and graphical ones on a simulator with --port each pick a free port. Older
    graphical simulators always bind 15423: each runs in its own network namespace (isolated.sh), or
    one at a time where there are none."""
    isolate = False
    if view == "graphical":
        mode = graphical_mode(sim_dir)
        isolate, jobs = mode == "netns", 1 if mode == "serial" else jobs
        log(f"graphical on {Path(sim_dir).name[:9]}: {GRAPHICAL_MODES[mode]}")

    def one(u):
        d = Path(root) / "units" / slug(u["id"])
        d.mkdir(parents=True, exist_ok=True)
        (d / "scenario.json").write_text(json.dumps(u["scenario"], indent=2))
        error = None
        try:
            skip = "--no-graphical" if view == "headless" else "--no-headless"
            cmd = ["nice", *([str(E2E_DIR / "isolated.sh")] if isolate else []), sys.executable,
                   str(E2E_DIR / "e2e.py"), "device", "--scenario", str(d / "scenario.json"),
                   "--sim-dir", str(sim_dir), "--out", str(d), "--label", u["id"], skip]
            env = dict(os.environ, **({"E2E_DRIVER": str(driver)} if driver else {}))
            with open(d / f"{view}.log", "w") as f:
                subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT, timeout=900, env=env)
        except subprocess.TimeoutExpired:
            error = "timeout"
        meta_path = d / "meta.json"
        meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
        if error is None and meta.get(f"{view}_ok") is None:
            error = f"no result, see {view}.log"  # e2e.py device crashed before writing meta.json
        if error:
            # The report shows why, instead of a bare failure.
            meta.update({f"{view}_ok": False, f"{view}_error": error})
            meta_path.write_text(json.dumps(meta, indent=2))
        return meta[f"{view}_ok"]

    t0 = time.time()
    with ThreadPoolExecutor(jobs) as ex:
        res = dict(zip([u["id"] for u in unit_list], ex.map(one, unit_list)))
    log(f"{view}: {sum(bool(v) for v in res.values())}/{len(res)} units ok in {time.time() - t0:.0f}s")
    return res
