"""Rabby extension -> BitBoxBridge (fake) -> graphical simulator, before and after a change to
Rabby, the firmware or bitbox-api-ts.

Builds outside the netns (Rabby, simulators, node caches), runs each flow in isolated.sh.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from bitbox_e2e import config, report
from bitbox_e2e.config import E2E_DIR, REPO_NAMES, REPOS, build_sim
from bitbox_e2e.report import CompareRow, FindingItem, Findings, ScreenChange, frame_rows

RABBY_DIR = E2E_DIR / "rabby"
RABBY_REPO = REPOS["rabby"].dir
DEFAULT_REF = "v0.94.15"
# Device setup: unlock, pairing code (random per run) and the idle screens around it.
SETUP_STEPS = {"boot", "pairing"}


def log(msg: str) -> None:
    config.log(msg, prefix="rabby")


def rabby_shots_equal(a: Path, b: Path, max_pixels: int = 64) -> bool:
    """Rabby screenshots, with a few pixels of slack: the green custom-RPC dot next to the chain
    icon anti-aliases differently from run to run (3-9 pixels). Device frames are
    compared exactly elsewhere."""
    from PIL import Image, ImageChops

    ia, ib = Image.open(a).convert("RGB"), Image.open(b).convert("RGB")
    if ia.size != ib.size:
        return False
    mask = ImageChops.difference(ia, ib).convert("L").point(lambda v: 255 if v else 0)
    return mask.histogram()[255] <= max_pixels


def ensure_rabby(ref: str, api_ts: str | None = None) -> Path:
    """Unpacked chrome-mv3 build of Rabby at ref, cached by commit in rabby-dist/<sha>, with
    bitbox-api-ts at api_ts in place of the release in its lockfile if given. All refs are built in
    one --shared clone (the working checkout is never touched), so node_modules exists once: two
    clones of it nearly filled a CI runner. A build takes ~10 min."""
    sha = config.resolve(ref, "rabby")
    ts_sha = config.resolve(api_ts, "api-ts") if api_ts else None
    dist = config.CACHE / "rabby-dist" / (sha + (f"-api-ts-{ts_sha}" if ts_sha else ""))
    if (dist / "manifest.json").exists():
        return dist
    src = config.CACHE / "src" / "rabby"
    if not (src / ".git").exists():
        subprocess.run(["git", "clone", "-q", "--shared", "--no-checkout", str(RABBY_REPO), str(src)], check=True)
    # -f: a build rewrites tracked files (e.g. the theme it generates), an injection package.json
    # and yarn.lock.
    subprocess.run(["git", "-C", str(src), "checkout", "-q", "-f", "--detach", sha], check=True)
    shutil.rmtree(src / "dist", ignore_errors=True)
    shutil.rmtree(src / "node_modules" / ".cache", ignore_errors=True)
    env = dict(os.environ)
    # Cap Node's V8 heap so Rabby's webpack build doesn't OOM-kill 16 GB CI runners.
    if "--max-old-space-size" not in env.get("NODE_OPTIONS", ""):
        env["NODE_OPTIONS"] = (env.get("NODE_OPTIONS", "") + " --max-old-space-size=5120").strip()
    if ts_sha:
        pkg = api_ts_package(ts_sha)
        manifest = json.loads((src / "package.json").read_text())
        manifest["dependencies"]["@bitboxswiss/bitbox-api"] = f"file:{pkg}"
        (src / "package.json").write_text(json.dumps(manifest, indent=2) + "\n")
        # A newer bitbox-api-ts may bring dependencies the lockfile lacks (CI=true makes yarn
        # refuse to change it otherwise).
        env["YARN_ENABLE_IMMUTABLE_INSTALLS"] = "false"
    yarn = re.search(r"^yarnPath:\s*(\S+)", (src / ".yarnrc.yml").read_text(), re.M).group(1)
    log(f"building Rabby {ref} ({sha[:9]})" + (f" with bitbox-api-ts {ts_sha[:9]}" if ts_sha else "") + f" in {src}")
    for script in ("install", "build:pro"):
        subprocess.run(["node", yarn, script], cwd=src, env=env, check=True, stdout=sys.stderr)
    tmp = dist.with_name(dist.name + ".tmp")
    shutil.rmtree(tmp, ignore_errors=True)
    shutil.copytree(src / "dist", tmp)
    tmp.rename(dist)
    return dist


def api_ts_package(sha: str) -> Path:
    """npm pack of bitbox-api-ts at sha (cached): what its release would publish."""
    _, src = config.clone_at("api-ts", sha)
    pkg = next(src.glob("bitboxswiss-bitbox-api-*.tgz"), None)
    if pkg:
        return pkg
    log(f"bitbox-api-ts {sha[:9]}: npm ci + pack")
    subprocess.run(
        ["npm", "ci", "--ignore-scripts", "--no-audit", "--no-fund"],
        cwd=src,
        check=True,
        stdout=sys.stderr,
    )
    subprocess.run(["npm", "run", "build"], cwd=src, check=True, stdout=sys.stderr)
    subprocess.run(["npm", "pack", "--ignore-scripts"], cwd=src, check=True, stdout=sys.stderr)
    return next(src.glob("bitboxswiss-bitbox-api-*.tgz"))


def ensure_tools() -> None:
    """Everything the netns run needs that would otherwise hit the network. Fails right away where
    the flow can't run at all."""
    config.use_node()
    # run.py listens on fixed ports (bridge, anvils, proxy, dapp) and must stay offline.
    if not config.namespaces():
        raise SystemExit("the Rabby flow runs in isolated.sh, which needs network namespaces; unavailable here")
    if not (RABBY_DIR / "node_modules" / "playwright").exists():
        subprocess.run(
            ["npm", "ci", "--ignore-scripts", "--no-audit", "--no-fund"],
            cwd=RABBY_DIR,
            check=True,
            stdout=sys.stderr,
        )
    subprocess.run(["npx", "playwright", "install", "chromium"], cwd=RABBY_DIR, check=True, stdout=sys.stderr)
    anvil = config.TOOLS / "foundry" / "bin" / "anvil"
    if not anvil.exists():
        raise SystemExit(f"anvil missing at {anvil}: run e2e/setup.sh")


def run_flow(sim_dir: Path, dist: Path, out: Path, record_sock: str | None = None) -> bool:
    cmd = [
        str(E2E_DIR / "isolated.sh"),
        sys.executable,
        str(RABBY_DIR / "run.py"),
        "--sim-dir",
        str(sim_dir),
        "--rabby-dist",
        str(dist),
        "--out",
        str(out),
    ]
    if record_sock:
        cmd += ["--record-sock", record_sock]
    log(f"flow: {out}")
    res = subprocess.run(cmd, env=dict(os.environ, UV_OFFLINE="1"))
    return res.returncode == 0


def record(firmware: str, rabby_ref: str, out: Path) -> None:
    """Record Rabby's API answers for this exact flow. The proxy runs on the host (internet), the
    browser in the netns reaches it through a unix socket (egress.py)."""
    ensure_tools()
    dist = ensure_rabby(rabby_ref)
    sim_dir = build_sim(firmware)
    out = Path(out)
    port = 18089
    if not config.is_port_free(port):
        raise SystemExit(f"127.0.0.1:{port} busy")
    fixture = out / "rabby-api.json.gz"
    out.mkdir(parents=True, exist_ok=True)
    fixture.unlink(missing_ok=True)
    # In a private directory: a predictable /tmp name could be claimed by another user first.
    sock_dir = Path(tempfile.mkdtemp(prefix="bitbox-e2e-rabby-"))
    sock = str(sock_dir / "proxy.sock")
    with open(out / "record-proxy.log", "w") as proxy_log:
        procs = [
            subprocess.Popen(
                [
                    config.venv_bin("mitmdump"),
                    "--listen-host",
                    "127.0.0.1",
                    "-p",
                    str(port),
                    "-s",
                    str(RABBY_DIR / "api_proxy.py"),
                    "--set",
                    f"fixture={fixture}",
                    "--set",
                    "record=true",
                ],
                stdout=proxy_log,
                stderr=subprocess.STDOUT,
            ),
            subprocess.Popen(
                [sys.executable, str(RABBY_DIR / "egress.py"), "unix-to-tcp", sock, f"127.0.0.1:{port}"]
            ),
        ]
    try:
        time.sleep(3)
        ok = run_flow(sim_dir, dist, out / "run", record_sock=sock)
    finally:
        for p in procs:
            p.terminate()
            p.wait(10)
        shutil.rmtree(sock_dir, ignore_errors=True)
    if not ok:
        raise SystemExit(f"recording run failed, fixture left at {fixture}")
    shutil.copy(fixture, RABBY_DIR / "fixtures" / "rabby-api.json.gz")
    log(f"fixture: {RABBY_DIR / 'fixtures' / 'rabby-api.json.gz'} ({fixture.stat().st_size} bytes)")


def compare(sides: dict, out: Path) -> int:
    """The flow on both sides (report.load_sides format, repos firmware, rabby, api-ts); returns 1 if
    a step that passed on base does not on head."""
    out = Path(out)
    ensure_tools()
    dists = {side: ensure_rabby(sides[side]["rabby"], sides[side].get("api-ts")) for side in ("base", "head")}
    sims = {side: build_sim(sides[side]["firmware"]) for side in ("base", "head")}
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)
    (out / "sides.json").write_text(json.dumps(sides, indent=1))
    for side in ("base", "head"):
        if not run_flow(sims[side], dists[side], out / side):
            log(f"flow failed on {side}")
    findings = render(out)
    for b in findings.broke:
        log(f"regression: {b.title}")
    return 1 if findings.broke else 0


# --- report ---------------------------------------------------------------------------------------


def load(side_dir: Path) -> dict:
    side_dir = Path(side_dir)

    def rd(name: str, default):
        return json.loads((side_dir / name).read_text()) if (side_dir / name).exists() else default

    events = (
        [json.loads(line) for line in (side_dir / "events.jsonl").read_text().splitlines()]
        if (side_dir / "events.jsonl").exists()
        else []
    )
    results: dict = {}
    for ev in events:
        if ev.get("event") == "step-end":
            results[ev["step"]] = ("error: " + ev["error"].splitlines()[0][:200]) if "error" in ev else "ok"
        elif ev.get("event") == "retry":
            results[ev["step"] + "#retry"] = results.get(ev["step"] + "#retry", 0) + 1
    return {
        "meta": rd("meta.json", {}),
        "frames": rd("frames.json", []),
        "shots": rd("shots.json", []),
        "results": results,
        "order": [e["step"] for e in events if e.get("event") == "step-start"],
    }


# What each step of flow.ts asks the device for, for the report.
STEPS = {
    "pairing": "Connect Rabby to the BitBox and pair",
    "eth-personal-sign": "Sign a message (personal_sign) on Ethereum",
    "eth-typed-data-v4": "Sign typed data (EIP-712) on Ethereum",
    "eth-send-1559": "Send ETH (EIP-1559) on Ethereum",
    "eth-erc20-transfer": "Send an ERC-20 token on Ethereum",
    "switch-arbitrum": "Switch the dapp to Arbitrum One",
    "arb-send-1559": "Send ETH (EIP-1559) on Arbitrum One",
    "arb-erc20-transfer": "Send an ERC-20 token on Arbitrum One",
    "switch-bsc": "Switch the dapp to BNB Chain",
    "bsc-send-legacy": "Send BNB (legacy transaction) on BNB Chain",
}


def render(out: Path) -> Findings:
    """Render report for a Rabby run, base vs head."""
    out = Path(out)
    b, h = load(out / "base"), load(out / "head")
    diffdir = out / "diff"
    steps = list(dict.fromkeys(h["order"] + b["order"] + list(STEPS)))
    side = {s: x["meta"] for s, x in (("base", b), ("head", h))}
    sides = report.load_sides(out) or {
        "changed": "rabby" if side["base"].get("rabby_ref") != side["head"].get("rabby_ref") else "firmware",
        **{s: {"firmware": side[s].get("firmware"), "rabby": side[s].get("rabby_ref")} for s in side},
    }
    changed_repo = sides["changed"]
    changed = REPO_NAMES[changed_repo]
    findings = Findings(
        repo=REPOS[changed_repo].slug,
        title=f"Rabby: {changed} {report.short_ref(sides['head'][changed_repo])}",
        sides=report.sides_header(sides),
        scope=(
            f"the Rabby browser extension signs with the BitBox02, with {report.sides_fixed(sides)}, "
            f"{changed} before and after this PR"
        ),
    )

    def step_rows(step: str) -> list[CompareRow]:
        fb = [x for x in b["frames"] if x["step"] == step]
        fh = [x for x in h["frames"] if x["step"] == step]
        rows = report.frame_compare(frame_rows(fb, fh, out / "base", out / "head", diffdir), out, "base", "head")
        for shot in [x for x in h["shots"] if x["step"] == step and x["what"] != "dapp"]:
            # Dapp-side shots show signatures (anti-klepto nonce) and never match; Rabby's own do.
            twin = next((x for x in b["shots"] if x["file"] == shot["file"]), None)
            if not twin or not rabby_shots_equal(out / "base" / twin["file"], out / "head" / shot["file"]):
                rows.append(
                    CompareRow(
                        label=f"Rabby: {shot['what']}",
                        before=f"base/{twin['file']}" if twin else None,
                        after=f"head/{shot['file']}",
                        photo=True,
                    )
                )
        return rows

    tested = []
    for i, step in enumerate(steps):
        rb, rh = b["results"].get(step), h["results"].get(step)
        text = STEPS.get(step, step)
        tested.append(f"{text} (`{step}`): before {rb or 'not run'}, after {rh or 'not run'}.")
        if rb == "ok" and rh not in (None, "ok"):
            later = [s for s in steps[i + 1 :] if s not in h["results"]]
            findings.broke.append(
                FindingItem(
                    title=f'Step "{text}" fails',
                    detail=(
                        f"It passed before this PR. With it: {rh.removeprefix('error: ')}."
                        + (f" Not run after this failure: {', '.join(f'`{s}`' for s in later)}." if later else "")
                    ),
                    compare=step_rows(step),
                )
            )
        elif rb not in (None, "ok") and rh == "ok":
            findings.fixed.append(f'Step "{text}" passes (before this PR: {rb.removeprefix("error: ")}).')
        elif rb not in (None, "ok") and rh not in (None, "ok"):
            findings.known.append(f"Step `{step}` fails before and after this PR: {rh.removeprefix('error: ')}")
        elif step not in SETUP_STEPS and rb == "ok" and rh == "ok" and (rows := step_rows(step)):
            findings.changes.append(
                ScreenChange(title=f'Screens changed in step "{text}"', where=[step], compare=rows)
            )
        if step not in SETUP_STEPS and rh == "ok":
            step_proof = [
                CompareRow(label=f"Rabby: {s['what']}", after=f"head/{s['file']}", photo=True)
                for s in h["shots"]
                if s["step"] == step and s["what"] != "dapp"
            ] + [
                CompareRow(label=f"Device #{idx + 1}", after=f"head/{fr['file']}")
                for idx, fr in enumerate(x for x in h["frames"] if x["step"] == step)
            ]
            if step_proof:
                findings.proof.append(ScreenChange(title=f'"{text}" (`{step}`)', where=[step], compare=step_proof))
    if not findings.broke and b["meta"].get("ok") and not h["meta"].get("ok"):
        err = h["meta"].get("error") or "The Rabby flow exited with an error before completing."
        findings.broke.append(
            FindingItem(
                title="The Rabby flow fails",
                detail=f"It passed before this PR. With it: {err}",
            )
        )
    elif (
        not findings.known
        and b["meta"].get("ok") is False
        and h["meta"].get("ok") is False
        and h["meta"].get("error")
    ):
        findings.known.append(f"The Rabby flow fails before and after this PR: {h['meta']['error']}")
    findings.tested = [
        "The Rabby browser extension signs with the BitBox02 simulator through a stand-in for the BitBoxBridge. "
        f"{changed} is before and after this PR; {report.sides_fixed(sides)} on both sides. Rabby's backend "
        "is replayed from a recording; the chains are local test chains.",
        "Device screens are compared pixel by pixel, Rabby's own screens with a few pixels of slack.",
        *tested,
    ]
    findings.details = [
        *report.sides_details(sides),
        ("simulator", side["base"].get("simulator_version", "-"), side["head"].get("simulator_version", "-")),
        ("run time (s)", str(side["base"].get("seconds", "-")), str(side["head"].get("seconds", "-"))),
    ]
    findings.notes = ["Logs and every frame: base/ and head/ (frames.json, events.jsonl)."]
    page = report.render_flow(out, findings)
    log(f"report: {page}")
    print(page)
    return findings
