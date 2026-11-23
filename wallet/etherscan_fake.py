"""mitmproxy addon: the BitBoxApp's Etherscan proxy (etherscan-api.shiftcrypto.io), answered from
local anvil chains. App runs get funded accounts, fixed fees and real broadcasts without the network.

It enforces the real proxy's request rules (etherscan_rules.py), because those are what the app's
client can get wrong (bitbox-wallet-app#4579).

Every request is logged as one JSON line to `requests_log`, so the report can say what the app
sent. Other hosts are not intercepted (start mitmdump with allow_hosts for this host only).

  mitmdump --listen-host 127.0.0.1 --mode socks5@PORT -s etherscan_fake.py \\
    --set chains=1=8645,11155111=8646 --set requests_log=F.jsonl --set allow_hosts='^etherscan-api\\.shiftcrypto\\.io'
"""

import json
import time
import urllib.request
from urllib.parse import parse_qsl, urlsplit

from mitmproxy import ctx, http

from etherscan_rules import check  # the script's directory is on sys.path under mitmdump

HOST = "etherscan-api.shiftcrypto.io"
FUNDS = 100 * 10**18
# Fixed gas market, in gwei like Etherscan's gas oracle; anvil's base fee is set to match.
GAS_ORACLE = {"SafeGasPrice": "1", "ProposeGasPrice": "1.5", "FastGasPrice": "2", "suggestBaseFee": "1",
              "gasUsedRatio": "0.5"}


def notok(message):
    return {"status": "0", "message": "NOTOK", "result": message}


def ok(result):
    return {"status": "1", "message": "OK", "result": result}


def proxy_call(action, p):
    """Etherscan `module=proxy` parameters as a JSON-RPC call."""
    tag = p.get("tag", "latest")
    tx = {k: p[k] for k in ("from", "to", "value", "gas", "gasPrice", "data") if k in p}
    return {
        "eth_blockNumber": ("eth_blockNumber", []),
        "eth_gasPrice": ("eth_gasPrice", []),
        "eth_getBlockByNumber": ("eth_getBlockByNumber", [tag, p.get("boolean") == "true"]),
        "eth_getTransactionByHash": ("eth_getTransactionByHash", [p.get("txhash")]),
        "eth_getTransactionReceipt": ("eth_getTransactionReceipt", [p.get("txhash")]),
        "eth_getTransactionCount": ("eth_getTransactionCount", [p.get("address"), tag]),
        "eth_getCode": ("eth_getCode", [p.get("address"), tag]),
        "eth_sendRawTransaction": ("eth_sendRawTransaction", [p.get("hex")]),
        "eth_call": ("eth_call", [tx, tag]),
        "eth_estimateGas": ("eth_estimateGas", [tx]),
    }.get(action)


class EtherscanFake:
    def __init__(self):
        self.ports = {}
        self.funded = set()
        self.sent = {}  # chain id -> tx hashes broadcast through here, for txlist

    def load(self, loader):
        loader.add_option("chains", str, "", "chain id=anvil port, comma separated")
        loader.add_option("requests_log", str, "", "JSON lines file, one line per request")

    def configure(self, updated):
        if "chains" in updated:
            self.ports = {c: int(p) for c, p in (x.split("=") for x in ctx.options.chains.split(",") if x)}

    def rpc(self, chain, method, params):
        req = urllib.request.Request(f"http://127.0.0.1:{self.ports[chain]}", json.dumps(
            {"jsonrpc": "2.0", "id": 83, "method": method, "params": params}).encode(),
            {"content-type": "application/json"})
        return json.loads(urllib.request.urlopen(req, timeout=30).read())

    def fund(self, chain, addresses):
        for a in addresses:
            if a and (chain, a.lower()) not in self.funded:
                self.funded.add((chain, a.lower()))
                if int(self.rpc(chain, "eth_getBalance", [a, "latest"])["result"], 16) == 0:
                    self.rpc(chain, "anvil_setBalance", [a, hex(FUNDS)])

    def txlist(self, chain, address):
        rows = []
        head = int(self.rpc(chain, "eth_blockNumber", [])["result"], 16)
        for h in self.sent.get(chain, []):
            tx = self.rpc(chain, "eth_getTransactionByHash", [h])["result"]
            rc = self.rpc(chain, "eth_getTransactionReceipt", [h])["result"]
            if not tx or not rc or address.lower() not in (tx["from"].lower(), (tx.get("to") or "").lower()):
                continue
            block = self.rpc(chain, "eth_getBlockByNumber", [tx["blockNumber"], False])["result"]
            n = int(tx["blockNumber"], 16)
            rows.append({"blockNumber": str(n), "timeStamp": str(int(block["timestamp"], 16)), "hash": h,
                         "nonce": str(int(tx["nonce"], 16)), "blockHash": tx["blockHash"],
                         "transactionIndex": str(int(tx["transactionIndex"], 16)), "from": tx["from"],
                         "to": tx.get("to") or "", "value": str(int(tx["value"], 16)), "gas": str(int(tx["gas"], 16)),
                         "gasPrice": str(int(rc["effectiveGasPrice"], 16)),
                         "isError": "0" if rc["status"] == "0x1" else "1", "txreceipt_status": str(int(rc["status"], 16)),
                         "input": tx["input"], "contractAddress": rc.get("contractAddress") or "",
                         "cumulativeGasUsed": str(int(rc["cumulativeGasUsed"], 16)),
                         "gasUsed": str(int(rc["gasUsed"], 16)), "confirmations": str(head - n + 1),
                         "methodId": tx["input"][:10] if len(tx["input"]) >= 10 else "0x", "functionName": ""})
        return ok(rows) if rows else {"status": "0", "message": "No transactions found", "result": []}

    def answer(self, chain, p):
        module, action = p.get("module"), p.get("action")
        if module == "proxy" and (call := proxy_call(action, p)):
            self.fund(chain, [p.get("address"), p.get("from")])
            res = self.rpc(chain, *call)
            if action == "eth_sendRawTransaction" and "result" in res:
                self.sent.setdefault(chain, []).append(res["result"])
            return {"jsonrpc": "2.0", "id": 83, **{k: res[k] for k in ("result", "error") if k in res}}
        if module == "account" and action in ("balance", "balancemulti"):
            addresses = p.get("address", "").split(",")
            self.fund(chain, addresses)
            bal = {a: str(int(self.rpc(chain, "eth_getBalance", [a, p.get("tag", "latest")])["result"], 16))
                   for a in addresses}
            return ok(bal[addresses[0]]) if action == "balance" else \
                ok([{"account": a, "balance": b} for a, b in bal.items()])
        if module == "account" and action == "txlist":
            self.fund(chain, [p.get("address")])
            return self.txlist(chain, p.get("address", ""))
        if module == "account" and action in ("tokentx", "txlistinternal"):
            return {"status": "0", "message": "No transactions found", "result": []}
        if module == "gastracker" and action == "gasoracle":
            return ok({"LastBlock": str(int(self.rpc(chain, "eth_blockNumber", [])["result"], 16)), **GAS_ORACLE})
        if module == "block" and action == "getblocknobytime":
            return ok(str(int(self.rpc(chain, "eth_blockNumber", [])["result"], 16)))
        return notok("Error! Missing Or invalid Action name")

    def request(self, flow: http.HTTPFlow):
        req = flow.request
        if req.pretty_host != HOST:
            return
        query = dict(parse_qsl(urlsplit(req.path).query, keep_blank_values=True))
        body = dict(parse_qsl(req.get_text() or "", keep_blank_values=True)) if req.method == "POST" else {}
        chain, given_in, error = check(req.method, req.path, query, body, self.ports)
        params = {**body, **query}
        entry = {"t": round(time.time(), 3), "method": req.method, "chain": chain, "chain_in": given_in,
                 "module": params.get("module"), "action": params.get("action"), "target_length": len(req.path)}
        if error and error.startswith("414"):
            flow.response = http.Response.make(414, b"<html><head><title>414 Request-URI Too Large</title></head></html>",
                                               {"content-type": "text/html"})
            entry["error"] = error
        else:
            if error:
                res = notok(error)
            else:
                try:
                    res = self.answer(chain, params)
                except Exception as e:  # noqa: BLE001 - an anvil failure must reach the app as a 500
                    flow.response = http.Response.make(500, str(e).encode())
                    entry["error"] = f"500 {e}"
                    self.log(entry)
                    return
            if res.get("status") == "0" and res.get("message") == "NOTOK":
                entry["error"] = res["result"]
            elif isinstance(res.get("error"), dict):
                entry["error"] = res["error"].get("message")
            flow.response = http.Response.make(200, json.dumps(res).encode(), {"content-type": "application/json"})
        self.log(entry)

    def log(self, entry):
        if ctx.options.requests_log:
            with open(ctx.options.requests_log, "a") as f:
                f.write(json.dumps(entry) + "\n")


addons = [EtherscanFake()]
