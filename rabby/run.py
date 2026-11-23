#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["pillow>=10", "python-xlib>=0.33"]
# ///
"""One Rabby flow against one graphical simulator. Run inside isolated.sh (fixed ports below).

  run.py --sim-dir <dir> --rabby-dist <dir> --out <dir> [--record-sock /tmp/x.sock]

Starts: graphical simulator (gsim) on its own Xvfb, sim_bridge.py on :8178, one anvil per chain, the
API proxy (api_proxy.py, replaying fixtures/rabby-api.json.gz, or recording through --record-sock to
a host-side proxy), the test dapp on :8080, and flow.ts with Chromium on a second Xvfb.
The device is recorded and confirmed by gsim.Recorder, as in every graphical run (tap top-right, then pinch).

Writes to --out: frames.json + device/*.png (device frames, labelled by flow step), shots.json +
rabby/*.png (Rabby screenshots), events.jsonl, logs.
"""

import argparse
import functools
import http.server
import json
import os
import queue
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

RABBY_DIR = Path(__file__).resolve().parent
E2E_DIR = RABBY_DIR.parent
sys.path.insert(0, str(E2E_DIR))

import bundle  # noqa: E402
import gsim  # noqa: E402

ANVIL = Path(os.environ.get("ANVIL", bundle.TOOLS / "foundry" / "bin" / "anvil"))
# chain id -> anvil port; flow.ts sets these as Rabby custom RPCs.
CHAINS = {1: 8545, 42161: 8546, 56: 8547}
# The account Rabby imports: m/44'/60'/0'/0/0 of simulator-graphical's --preseed mnemonic. Funded
# before the browser starts, so it can't come from Rabby; flow.ts reports the address Rabby got
# and a different one fails the run (every transfer would hit an unfunded account).
ADDRESS = "0x416e88840eb6353e49252da2a2c140ea1f969d1a"
# Real token addresses get TestToken's code (see fixtures/TestToken.sol).
TOKENS = {1: "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48", 42161: "0xaf88d065e77c8cC2239327C5EDB3A432268e5831"}
PROXY_PORT = 18088
DAPP_PORT = 8080
FIXTURE = RABBY_DIR / "fixtures" / "rabby-api.json.gz"
MITMDUMP = RABBY_DIR / "mitmdump.py"
# Device setup: unlock, pairing code (random per run) and the idle screens around it.
SETUP_STEPS = {"boot", "pairing"}


def log(msg):
    print(f"[rabby] {msg}", file=sys.stderr, flush=True)


def rpc(port, method, params):
    req = urllib.request.Request(f"http://127.0.0.1:{port}", json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode(),
        {"content-type": "application/json"})
    res = json.loads(urllib.request.urlopen(req, timeout=10).read())
    if "error" in res:
        raise RuntimeError(f"{method}: {res['error']}")
    return res["result"]


def wait_port(port, timeout_s=30):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.2)
    raise RuntimeError(f"nothing listening on 127.0.0.1:{port}")


def start_anvils(address, logdir):
    procs = []
    code = (RABBY_DIR / "fixtures" / "TestToken.runtime.hex").read_text().strip()
    for chain_id, port in CHAINS.items():
        # Base fee 0: Rabby's gas prices come from the recorded mainnet gas market and may be
        # below anvil's default 1 gwei, which anvil would reject.
        procs.append(subprocess.Popen(
            [str(ANVIL), "--chain-id", str(chain_id), "--port", str(port), "--block-base-fee-per-gas", "0"],
            stdout=open(logdir / f"anvil-{chain_id}.log", "w"), stderr=subprocess.STDOUT))
    for chain_id, port in CHAINS.items():
        deadline = time.time() + 30
        while True:
            try:
                rpc(port, "eth_chainId", [])
                break
            except OSError:
                if time.time() > deadline:
                    raise
                time.sleep(0.2)
        rpc(port, "anvil_setBalance", [address, hex(100 * 10**18)])
        if chain_id in TOKENS:
            rpc(port, "anvil_setCode", [TOKENS[chain_id], code])
    return procs




def serve_dapp():
    class Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *a):
            pass

    handler = functools.partial(Quiet, directory=str(RABBY_DIR / "dapp"))
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", DAPP_PORT), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def pump(stream, q):
    for line in iter(stream.readline, ""):
        q.put(line.rstrip("\n"))
    q.put(None)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--sim-dir", required=True)
    p.add_argument("--rabby-dist", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--record-sock", help="record the API fixture through this host-side proxy socket")
    p.add_argument("--timeout", type=int, default=900)
    args = p.parse_args()
    # Turn SIGTERM into an exception so the finally block below tears everything down.
    signal.signal(signal.SIGTERM, lambda *a: sys.exit(143))

    out = Path(args.out)
    shutil.rmtree(out, ignore_errors=True)
    (out / "device").mkdir(parents=True)
    (out / "logs").mkdir()
    logs = out / "logs"
    env = dict(os.environ, UV_OFFLINE="1")
    procs, cleanup = [], []
    t0 = time.time()
    events, shots = [], []
    rec = None
    ok = False
    error = None
    try:
        xvfb = gsim.Xvfb(logs / "xvfb-sim.log")
        cleanup.append(xvfb.stop)
        bxvfb = gsim.Xvfb(logs / "xvfb-browser.log")
        cleanup.append(bxvfb.stop)
        memdir = out / "fake-memory"
        memdir.mkdir()
        # 15423 (no --port): sim_bridge.py's default, and this runs in its own network namespace.
        sim = gsim.GraphicalSim(str(Path(args.sim_dir) / "simulator-graphical"), xvfb.disp,
                                str(logs / "simulator.log"), str(memdir))
        cleanup.append(sim.stop)
        rec = gsim.Recorder(sim, out / "device", SETUP_STEPS, prefix="device/")

        procs.append(subprocess.Popen([str(E2E_DIR / "bridge" / "sim_bridge.py"), "--log", str(logs / "bridge.log")],
                                      env=env, stdout=subprocess.DEVNULL, stderr=open(logs / "bridge.err", "w")))
        procs += start_anvils(ADDRESS, logs)
        if args.record_sock:
            procs.append(subprocess.Popen([sys.executable, str(RABBY_DIR / "egress.py"), "tcp-to-unix",
                                           f"127.0.0.1:{PROXY_PORT}", args.record_sock]))
        else:
            procs.append(subprocess.Popen(
                [str(MITMDUMP), "--listen-host", "127.0.0.1",
                 "-p", str(PROXY_PORT), "-s", str(RABBY_DIR / "api_proxy.py"), "--set", f"fixture={FIXTURE}",
                 # Lazy: don't resolve/connect upstream on CONNECT (there is no upstream in here).
                 "--set", "connection_strategy=lazy"],
                env=env, stdout=open(logs / "api-proxy.log", "w"), stderr=subprocess.STDOUT))
        dapp = serve_dapp()
        cleanup.append(dapp.shutdown)
        wait_port(8178)
        wait_port(PROXY_PORT, timeout_s=60)

        flow = subprocess.Popen(
            ["node", str(RABBY_DIR / "flow.ts"), "--ext", str(Path(args.rabby_dist).resolve()), "--out", str(out),
             "--dapp", f"http://127.0.0.1:{DAPP_PORT}/", "--proxy", f"127.0.0.1:{PROXY_PORT}"],
            cwd=RABBY_DIR, env=dict(env, DISPLAY=bxvfb.disp), text=True,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=open(logs / "flow.err", "w"))
        procs.append(flow)
        q = queue.Queue()
        threading.Thread(target=pump, args=(flow.stdout, q), daemon=True).start()

        deadline = time.time() + args.timeout
        done = False
        while time.time() < deadline:
            while not q.empty():
                line = q.get()
                if line is None:
                    done = True
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ev["t"] = round(time.time() - t0, 2)
                events.append(ev)
                if ev.get("event") == "step-start":
                    rec.step = ev["step"]
                    flow.stdin.write("go\n")
                    flow.stdin.flush()
                elif ev.get("event") == "shot":
                    shots.append({k: ev[k] for k in ("step", "file", "what")})
                elif ev.get("event") == "step-end" and ev.get("step") == "pairing" and "result" in ev:
                    if ev["result"].get("address", "").lower() != ADDRESS:
                        error = f"Rabby imported {ev['result'].get('address')}, anvil funded {ADDRESS}"
                        log(error)
                elif ev.get("event") == "step-end" and "error" in ev:
                    log(f"step {ev['step']} failed: {ev['error'].splitlines()[0]}")
                elif ev.get("event") == "done":
                    done = True
                    ok = "error" not in ev
            if rec.settle():
                continue
            if done:
                break
            rec.act()
        else:
            log("timeout")
        flow.wait(30)
        rec.settle(1.0)
    finally:
        for pr in reversed(procs):
            if pr.poll() is None:
                pr.terminate()
        for pr in procs:
            try:
                pr.wait(10)
            except subprocess.TimeoutExpired:
                pr.kill()
        for fn in reversed(cleanup):
            try:
                fn()
            except Exception as e:  # keep tearing down the rest
                log(f"cleanup: {e}")
        shutil.rmtree(out / "fake-memory", ignore_errors=True)
        shutil.rmtree(out / "profile", ignore_errors=True)

    keep = rec.finish(out / "frames.json") if rec else []
    (out / "shots.json").write_text(json.dumps(shots, indent=2))
    (out / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))
    ok = ok and error is None
    meta = {"ok": ok, "error": error, "seconds": round(time.time() - t0, 1), "simulator": str(args.sim_dir),
            "source": (Path(args.sim_dir) / "source.txt").read_text().strip() if (Path(args.sim_dir) / "source.txt").exists() else "",
            "simulator_version": (Path(args.sim_dir) / "version.txt").read_text().strip() if (Path(args.sim_dir) / "version.txt").exists() else "",
            "rabby_dist": str(args.rabby_dist), "fixture": "record" if args.record_sock else str(FIXTURE)}
    (out / "meta.json").write_text(json.dumps(meta, indent=2))
    log(f"{'ok' if ok else 'FAILED'}: {len(keep)} device frames, {len(shots)} screenshots, {meta['seconds']}s")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
