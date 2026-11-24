"""Local chains for the BitBoxApp: one anvil per chain, and the app's Etherscan proxy answered from
them (etherscan_fake.py) behind a SOCKS5 proxy, which the app's own proxy setting points at.

Only the Etherscan host is intercepted; everything else the app reaches through the SOCKS proxy
(WalletConnect relay lookups, BTC servers, exchange rates) passes through to the network.
"""

import contextlib
import os
import subprocess
import time
from pathlib import Path

from bitbox_e2e import config

WALLET_DIR = Path(__file__).resolve().parent
ANVIL = Path(os.environ.get("ANVIL", config.TOOLS / "foundry" / "bin" / "anvil"))
# Chain id -> anvil port: mainnet, Sepolia and the L2s the app offers over WalletConnect.
CHAINS = {1: 8645, 11155111: 8646, 42161: 8647, 137: 8648, 10: 8649, 8453: 8650, 56: 8651}
SOCKS_PORT = 8660
SYSTEM_CAS = ("/etc/ssl/certs/ca-certificates.crt", "/etc/pki/tls/certs/ca-bundle.crt")


def wait_for(check, what, timeout_s=30):
    deadline = time.time() + timeout_s
    while True:
        try:
            if check():
                return
        except OSError:
            pass
        if time.time() > deadline:
            raise RuntimeError(f"{what} did not come up in {timeout_s}s")
        time.sleep(0.2)


@contextlib.contextmanager
def local_chains(out):
    """Starts the chains and the proxy, logs into out/. Yields {"socks": host:port, "ca_file": a CA
    bundle that also trusts the proxy (for SSL_CERT_FILE), "rpc": {chain id: url},
    "requests_log": the proxy's request log}."""
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    busy = [p for p in (*CHAINS.values(), SOCKS_PORT) if not config.is_port_free(p)]
    if busy:
        raise RuntimeError(f"ports {busy} busy on 127.0.0.1; is another app run going?")
    procs = []
    try:
        for chain_id, port in CHAINS.items():
            with open(out / f"anvil-{chain_id}.log", "w") as log_f:
                procs.append(
                    subprocess.Popen(
                        [
                            str(ANVIL),
                            "--chain-id",
                            str(chain_id),
                            "--port",
                            str(port),
                            "--host",
                            "127.0.0.1",
                            "--block-base-fee-per-gas",
                            str(10**9),
                        ],
                        stdout=log_f,
                        stderr=subprocess.STDOUT,
                    )
                )
        for port in CHAINS.values():
            wait_for(lambda p=port: config.json_rpc_result(p, "eth_chainId", []), f"anvil on {port}")
        confdir = out / "mitmproxy"
        requests_log = out / "etherscan.jsonl"
        requests_log.unlink(missing_ok=True)
        with open(out / "etherscan-proxy.log", "w") as log_f:
            procs.append(
                subprocess.Popen(
                    [
                        config.venv_bin("mitmdump"),
                        "--listen-host",
                        "127.0.0.1",
                        "--mode",
                        f"socks5@{SOCKS_PORT}",
                        "-s",
                        str(WALLET_DIR / "etherscan_fake.py"),
                        "--set",
                        f"confdir={confdir}",
                        "--set",
                        "chains=" + ",".join(f"{c}={p}" for c, p in CHAINS.items()),
                        "--set",
                        f"requests_log={requests_log}",
                        "--set",
                        r"allow_hosts=^etherscan-api\.shiftcrypto\.io(:443)?$",
                        # Answer from the certificate mitmproxy makes up, without contacting the real host.
                        "--set",
                        "upstream_cert=false",
                        "--set",
                        "connection_strategy=lazy",
                    ],
                    stdout=log_f,
                    stderr=subprocess.STDOUT,
                )
            )
        ca = confdir / "mitmproxy-ca-cert.pem"
        wait_for(lambda: ca.exists() and not config.is_port_free(SOCKS_PORT), "the Etherscan proxy", 60)
        system = next((Path(p) for p in SYSTEM_CAS if Path(p).exists()), None)
        if system is None:
            raise RuntimeError(f"no system CA bundle at {' or '.join(SYSTEM_CAS)}")
        ca_file = out / "ca-bundle.pem"
        ca_file.write_text(system.read_text() + "\n" + ca.read_text())
        yield {"socks": f"127.0.0.1:{SOCKS_PORT}", "ca_file": str(ca_file), "requests_log": str(requests_log),
               "rpc": {c: f"http://127.0.0.1:{p}" for c, p in CHAINS.items()}}
    finally:
        for p in procs:
            p.terminate()
        for p in procs:
            try:
                p.wait(10)
            except subprocess.TimeoutExpired:
                p.kill()


def app_config(socks):
    """The app's config.json that routes its traffic through the SOCKS proxy."""
    return {"backend": {"proxy": {"useProxy": True, "proxyAddress": socks}}}
