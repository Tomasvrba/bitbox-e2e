"""Shared configuration, repo metadata, git/bundle helpers, port/RPC utilities, and runtime probes."""

import functools
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import NamedTuple

# Same defaults as env.sh, for runs that did not source it.
E2E_DIR = Path(__file__).resolve().parent.parent
WORKSPACE = Path(os.environ.get("BITBOX_WORKSPACE", E2E_DIR.parent))
CACHE = Path(os.environ.get("BITBOX_E2E_CACHE", Path.home() / ".cache" / "bitbox-e2e"))
TOOLS = Path(
    os.environ.get(
        "BITBOX_E2E_TOOLS",
        Path(os.environ.get("BITBOX_E2E_HOME", Path.home() / ".local" / "share" / "bitbox-e2e")) / "tools",
    )
)
FW = Path(os.environ.get("BITBOX_FIRMWARE", WORKSPACE / "bitbox02-firmware"))
SCENARIOS = E2E_DIR / "scenarios"

# Where the API simulator suites, the app's simulator mode, and graphical simulators without --port
# connect. Everything else gets a free port.
SIM_PORT = 15423
# A graphical simulator busy-loops a core and misses host polls under load; headless units would
# take more, but one -j serves both.
JOBS = max(1, len(os.sched_getaffinity(0)) // 2)
# Bump when a bundle recorded by an older harness lacks data the current one relies on: such a
# bundle is then recorded again instead of being read with fallbacks.
BUNDLE_FORMAT = 3
FW_VECTORS = "src/rust/bitbox-test-vectors/testdata/btc-transaction-test-vectors.json"


class Repo(NamedTuple):
    dir: Path
    slug: str  # on GitHub
    branch: str  # default branch


# The repos a change under test can be in (`e2e pr --repo`), checked out next to e2e/.
REPOS = {
    "firmware": Repo(FW, "BitBoxSwiss/bitbox02-firmware", "master"),
    "app": Repo(
        Path(os.environ.get("BITBOX_WALLET_APP", WORKSPACE / "bitbox-wallet-app")),
        "BitBoxSwiss/bitbox-wallet-app",
        "master",
    ),
    "api-rs": Repo(WORKSPACE / "bitbox-api-rs", "BitBoxSwiss/bitbox-api-rs", "master"),
    "api-go": Repo(WORKSPACE / "bitbox02-api-go", "BitBoxSwiss/bitbox02-api-go", "master"),
    "api-ts": Repo(WORKSPACE / "bitbox-api-ts", "BitBoxSwiss/bitbox-api-ts", "master"),
    "rabby": Repo(
        Path(os.environ.get("BITBOX_RABBY", WORKSPACE / "Rabby")),
        "RabbyHub/Rabby",
        "develop",
    ),
}
# Repos that --pin can fix for both sides: any but the one under test.
PIN_REPOS = tuple(REPOS)
API_REPOS = ("api-rs", "api-go", "api-ts")

# What a change in each repo runs. "device": every unit and the API suites. The others are the
# integrations built on top of the BitBox repos: the app's own tests, a dapp over WalletConnect
# through the app, and the Rabby extension.
FLOWS = {
    "firmware": ("device", "app", "wc", "rabby"),
    "api-rs": ("device",),
    "api-go": ("device", "app", "wc"),
    "api-ts": ("device", "rabby"),
    "app": ("app", "wc"),
    "rabby": ("rabby",),
}
# The repos each integration flow builds on. The changed one is at base and head, the others at
# --pin or their defaults on both sides; None is the version the app or Rabby ships with.
FLOW_REPOS = {
    "app": ("firmware", "app", "api-go"),
    "wc": ("firmware", "app", "api-go"),
    "rabby": ("firmware", "rabby", "api-ts"),
}
FLOW_NAMES = {
    "device": "Device tests",
    "app": "BitBoxApp tests",
    "wc": "WalletConnect",
    "rabby": "Rabby",
}
FLOW_ORDER = ("device", "app", "wc", "rabby")
REPO_NAMES = {
    "firmware": "firmware",
    "app": "BitBoxApp",
    "api-rs": "bitbox-api-rs",
    "api-go": "bitbox02-api-go",
    "api-ts": "bitbox-api-ts",
    "rabby": "Rabby",
}
SHIPPED = {
    "api-go": "the version the app vendors",
    "api-ts": "the version Rabby's lockfile pins",
}


def log(msg: str, prefix: str = "e2e") -> None:
    print(f"[{prefix}] {msg}", file=sys.stderr, flush=True)


def read_text(p: Path | str) -> str:
    path = Path(p)
    return path.read_text().strip() if path.exists() else ""


def slug(unit_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", unit_id.replace("/", "--"))


# --- git / repo helpers ---------------------------------------------------------------------------


def git(*args: str, cwd: Path | str | None = None) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd or FW), *args],
        stdout=subprocess.PIPE,
        text=True,
        check=True,
    ).stdout.strip()


def resolve(ref: str, repo: str = "firmware") -> str:
    """Commit sha of ref in one of REPOS. WORKTREE is the firmware checkout's HEAD (build-sim.sh
    builds its files as they are)."""
    cwd = REPOS[repo].dir
    if ref == "WORKTREE":
        return git("rev-parse", "HEAD", cwd=cwd)
    return git("rev-parse", "--verify", f"{ref}^{{commit}}", cwd=cwd)


def checkout_rev(path: Path | str) -> str:
    """HEAD of the git checkout at path, '+dirty' with uncommitted changes under it."""
    dirty = git("status", "--porcelain", "--", ".", cwd=path)
    return git("rev-parse", "HEAD", cwd=path) + ("+dirty" if dirty else "")


def harness_sha() -> str:
    return checkout_rev(E2E_DIR)


def upstream_remote(repo_dir: Path | str, github_slug: str) -> str:
    """The remote of repo_dir pointing at github.com/<github_slug>, else 'origin'. Remote names are
    per clone: a CI checkout only has origin, a developer's origin is often their fork."""
    url_re = re.compile(rf"github\.com[:/]{re.escape(github_slug)}(\.git)?/?$", re.I)
    for line in git("remote", "-v", cwd=repo_dir).splitlines():
        name, url = line.split()[:2]
        if url_re.search(url):
            return name
    return "origin"


def default_ref(repo: str) -> str:
    """The default branch of the upstream repo, e.g. bitbox/master in a developer's firmware clone."""
    r = REPOS[repo]
    return f"{upstream_remote(r.dir, r.slug)}/{r.branch}"


def default_firmware_ref() -> str:
    return default_ref("firmware")


def clone_at(repo: str, ref: str) -> tuple[str, Path]:
    """A clean checkout of one of REPOS at ref, kept in the cache by commit (the working checkout is
    never touched). Returns (sha, path)."""
    sha = resolve(ref, repo)
    src = CACHE / "src" / f"{REPOS[repo].dir.name}-{sha}"
    if not (src / ".git").exists():
        tmp = src.with_name(src.name + ".tmp")
        shutil.rmtree(tmp, ignore_errors=True)
        subprocess.run(
            ["git", "clone", "-q", "--shared", "--no-checkout", str(REPOS[repo].dir), str(tmp)],
            check=True,
        )
        subprocess.run(["git", "-C", str(tmp), "checkout", "-q", "--detach", sha], check=True)
        tmp.rename(src)
    return sha, src


def firmware_dir(ref: str) -> Path:
    """A directory load_units can read the firmware's files from. WORKTREE is the checkout itself;
    for commits only the files units need are materialized, no full checkout."""
    if ref == "WORKTREE":
        return FW
    sha = resolve(ref)
    d = CACHE / "src" / f"fw-files-{sha}"
    dst = d / FW_VECTORS
    d.mkdir(parents=True, exist_ok=True)
    if not dst.exists():
        # Refs before the vectors file (firmware 3bdaf583b) have no btc-vector units.
        r = subprocess.run(
            ["git", "-C", str(FW), "show", f"{sha}:{FW_VECTORS}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        if r.returncode == 0:
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_text(r.stdout + "\n")
    return d


def has_vectors(fw_dir: Path | str) -> bool:
    return (Path(fw_dir) / FW_VECTORS).exists()


# --- bundle / simulator helpers -------------------------------------------------------------------


def bundle_dir(sha: str, revs: dict | None = None) -> Path:
    """Bundle of a firmware commit. revs pins other repos ({'api-rs': sha, ...}); those bundles are
    kept apart from the default one, which uses whatever the sibling checkouts had."""
    if not revs:
        return CACHE / "baselines" / sha
    key = hashlib.sha256(json.dumps(revs, sort_keys=True).encode()).hexdigest()[:12]
    return CACHE / "baselines" / f"{sha}-{key}"


def read_manifest(root: Path | str) -> dict | None:
    """The manifest of a complete bundle of the current format, else None."""
    p = Path(root) / "manifest.json"
    m = json.loads(p.read_text()) if p.exists() else None
    return m if m and m.get("format") == BUNDLE_FORMAT else None


def write_manifest(root: Path | str, m: dict) -> None:
    (Path(root) / "manifest.json").write_text(json.dumps(m | {"format": BUNDLE_FORMAT}, indent=2))


def build_sim(ref: str) -> Path:
    """Simulators for a firmware ref (build-sim.sh, cached by commit); returns their directory."""
    log(f"building simulators for {ref}")
    res = subprocess.run(
        [str(E2E_DIR / "build-sim.sh"), ref],
        stdout=subprocess.PIPE,
        text=True,
        check=True,
    )
    return Path(res.stdout.strip().splitlines()[-1])


def sim_version(sim_dir: Path | str) -> str:
    """The simulator's release name; expectations are picked by the version in it."""
    v = read_text(Path(sim_dir) / "version.txt")
    if not v:
        raise RuntimeError(f"{sim_dir}/version.txt is missing or empty; rebuild it with build-sim.sh")
    return v


# --- port & JSON-RPC helpers ----------------------------------------------------------------------


def free_port() -> int:
    """A port a simulator can bind right now. Bound like the headless simulator binds (0.0.0.0, no
    SO_REUSEADDR), so the kernel does not hand out a port that is in TIME_WAIT."""
    with socket.socket() as s:
        s.bind(("0.0.0.0", 0))
        return s.getsockname()[1]


def is_port_free(port: int) -> bool:
    """Whether nothing is currently accepting TCP connections on 127.0.0.1:<port>."""
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) != 0


def wait_port_free(port: int, timeout_s: float = 70, reuseaddr: bool = False) -> None:
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


def wait_tcp(port: int, timeout_s: float = 30) -> None:
    """Waits until something accepts TCP connections on 127.0.0.1:<port>."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if not is_port_free(port):
            return
        time.sleep(0.2)
    raise RuntimeError(f"nothing listening on 127.0.0.1:{port}")


def json_rpc(port: int, method: str, params: list, req_id: int = 1, timeout: float = 10) -> dict:
    """Send a JSON-RPC 2.0 request to http://127.0.0.1:<port> and return the decoded response."""
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}",
        json.dumps({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}).encode(),
        {"content-type": "application/json"},
    )
    return json.loads(urllib.request.urlopen(req, timeout=timeout).read())


def json_rpc_result(port: int, method: str, params: list, timeout: float = 10):
    """Send a JSON-RPC 2.0 request, raising RuntimeError on error, and return res['result']."""
    res = json_rpc(port, method, params, timeout=timeout)
    if "error" in res:
        raise RuntimeError(f"{method}: {res['error']}")
    return res["result"]


# --- runtime helpers ------------------------------------------------------------------------------


@functools.cache
def namespaces() -> bool:
    """Whether isolated.sh works here. Most CI containers block unprivileged user namespaces."""
    r = subprocess.run(
        [str(E2E_DIR / "isolated.sh"), "true"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    if r.returncode:
        log("no network namespaces here, fixed-port runs go one at a time: " + " ".join(r.stderr.split()[:30]))
    return r.returncode == 0


@functools.cache
def takes_port(sim_dir: Path | str) -> bool:
    """Whether this simulator-graphical has --port. Firmware before a540c993b always binds 15423."""
    try:
        r = subprocess.run(
            [str(Path(sim_dir) / "simulator-graphical"), "--help"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=30,
        )
    except OSError:
        return False
    return "--port" in r.stdout


# How graphical units run on a simulator, see graphical_mode().
GRAPHICAL_MODES = {
    "port": "parallel, own --port each",
    "netns": "parallel, one network namespace each (simulator has no --port)",
    "serial": f"one at a time on {SIM_PORT} (simulator has no --port, no network namespaces here)",
}


def graphical_mode(sim_dir: Path | str) -> str:
    if takes_port(sim_dir):
        return "port"
    return "netns" if namespaces() else "serial"


def use_node() -> None:
    """Puts node 26 (env.sh's BITBOX_NODE_BIN) first on PATH for every child process. The app needs
    >=26.12.1; another node fails late and confusingly, so refuse to start without it."""
    node_bin = os.environ.get("BITBOX_NODE_BIN")
    if not node_bin:
        sys.exit(
            "BITBOX_NODE_BIN is unset, so there is no node 26: run e2e/setup.sh (it installs one), "
            "then source e2e/env.sh"
        )
    if os.environ.get("PATH", "").split(os.pathsep)[0] != node_bin:
        os.environ["PATH"] = node_bin + os.pathsep + os.environ.get("PATH", "")


def venv_bin(name: str) -> str:
    """Path to an executable in the active Python environment's bin dir, else on PATH."""
    candidate = Path(sys.executable).parent / name
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return str(candidate)
    return shutil.which(name) or name
