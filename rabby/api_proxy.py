"""mitmproxy addon: Rabby's own backend (api.rabby.io, static.debank.com) from a recorded fixture.

Rabby asks its backend for chain lists, gas prices, simulation (pre-exec), security checks, token
info and balances. Runs are offline (isolated.sh) and must be deterministic, so the browser goes
through this proxy (--proxy-server, --ignore-certificate-errors) and gets recorded responses.

  replay: mitmdump -s api_proxy.py --set fixture=F.json.gz
  record: mitmdump -s api_proxy.py --set fixture=F.json.gz --set record=true   (needs the internet)

Lookup is by method + host + path + query + JSON body (keys sorted), falling back to method + host +
path, so a request whose body differs slightly from the recording (e.g. another nonce) still gets
the recorded answer. The first recorded response per key wins, independent of call order.
Hosts other than Rabby's are dropped (analytics, Chrome component updates), also when recording.
Unmatched requests get a connection reset, which is what Rabby sees offline.
"""

import base64
import gzip
import json
import logging
import os
from urllib.parse import parse_qsl, urlsplit

from mitmproxy import ctx, http

HOSTS = ("rabby.io", "debank.com")
KEEP_HEADERS = ("content-type", "access-control-allow-origin")
# Big and irrelevant to signing (3 MB of gas-account bridge tokens); Rabby copes with it failing.
DROP_PATHS = ("/v1/gas_account/bridge/support_token",)


def exact_key(req):
    u = urlsplit(req.path)
    # The extension id is part of some queries; it depends on the unpacked extension's path.
    query = sorted((k, v) for k, v in parse_qsl(u.query, keep_blank_values=True)
                   if not v.startswith("chrome-extension://"))
    body = req.content or b""
    try:
        body = json.dumps(json.loads(body), sort_keys=True, separators=(",", ":"))
    except ValueError:
        body = body.decode("latin-1")
    return json.dumps([req.method, req.pretty_host, u.path, query, body])


def loose_key(req):
    return json.dumps([req.method, req.pretty_host, urlsplit(req.path).path])


class Fixture:
    def __init__(self):
        self.entries = {}

    def load(self, loader):
        loader.add_option("fixture", str, "", "recorded responses (.json.gz)")
        loader.add_option("record", bool, False, "forward to the real hosts and record")

    def configure(self, updated):
        if "fixture" in updated and ctx.options.fixture and os.path.exists(ctx.options.fixture):
            with gzip.open(ctx.options.fixture, "rt") as f:
                self.entries = json.load(f)
            logging.info(f"fixture: {len(self.entries)} entries")

    def request(self, flow: http.HTTPFlow):
        if not flow.request.pretty_host.endswith(HOSTS) or urlsplit(flow.request.path).path in DROP_PATHS:
            flow.kill()
            return
        if ctx.options.record:
            return
        e = self.entries.get(exact_key(flow.request)) or self.entries.get(loose_key(flow.request))
        if e and "ref" in e:
            e = self.entries[e["ref"]]
        if e is None:
            logging.warning(f"fixture miss: {flow.request.method} {flow.request.pretty_url}")
            flow.kill()
            return
        body = base64.b64decode(e["b64"]) if "b64" in e else e["text"].encode()
        flow.response = http.Response.make(e["status"], body, e["headers"])

    def response(self, flow: http.HTTPFlow):
        if not ctx.options.record or not flow.response:
            return
        r = flow.response
        e = {"status": r.status_code,
             "headers": {k: v for k, v in r.headers.items() if k.lower() in KEEP_HEADERS}}
        try:
            e["text"] = r.content.decode()
        except UnicodeDecodeError:
            e["b64"] = base64.b64encode(r.content).decode()
        exact, loose = exact_key(flow.request), loose_key(flow.request)
        if exact not in self.entries:
            self.entries[exact] = e
        # The fallback points at the first exact match instead of holding a second copy.
        self.entries.setdefault(loose, {"ref": exact})

    def done(self):
        if ctx.options.record:
            self.save()

    def save(self):
        tmp = ctx.options.fixture + ".tmp"
        with gzip.open(tmp, "wt", compresslevel=6) as f:
            json.dump(self.entries, f, sort_keys=True, indent=0)
        os.replace(tmp, ctx.options.fixture)
        logging.info(f"fixture: wrote {len(self.entries)} entries")


addons = [Fixture()]
