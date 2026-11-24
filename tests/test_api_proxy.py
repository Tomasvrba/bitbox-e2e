"""Contract tests for rabby/api_proxy.py request normalization, filtering, and replay lookup."""

import base64
import importlib.util
from pathlib import Path
from types import SimpleNamespace

_SPEC = importlib.util.spec_from_file_location(
    "api_proxy",
    Path(__file__).resolve().parent.parent / "rabby" / "api_proxy.py",
)
api_proxy = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(api_proxy)


class FakeRequest:
    def __init__(self, method: str, pretty_host: str, path: str, content: bytes = b""):
        self.method = method
        self.pretty_host = pretty_host
        self.path = path
        self.content = content
        self.pretty_url = f"https://{pretty_host}{path}"


class FakeFlow:
    def __init__(self, request: FakeRequest):
        self.request = request
        self.response = None
        self.killed = False

    def kill(self):
        self.killed = True


def _replay_fixture(monkeypatch, entries: dict) -> api_proxy.Fixture:
    monkeypatch.setattr(api_proxy.ctx, "options", SimpleNamespace(record=False, fixture=""), raising=False)
    fx = api_proxy.Fixture()
    fx.entries = entries
    return fx


def test_exact_key_normalizes_json_body_key_order():
    r1 = FakeRequest("POST", "api.rabby.io", "/v1/tx/pre_exec", b'{"b": 2, "a": {"y": 1, "x": 0}}')
    r2 = FakeRequest("POST", "api.rabby.io", "/v1/tx/pre_exec", b'{"a":{"x":0,"y":1},"b":2}')
    r3 = FakeRequest("POST", "api.rabby.io", "/v1/tx/pre_exec", b'{"a":{"x":9,"y":1},"b":2}')
    assert api_proxy.exact_key(r1) == api_proxy.exact_key(r2)
    assert api_proxy.exact_key(r1) != api_proxy.exact_key(r3)


def test_exact_key_strips_chrome_extension_query_params():
    r1 = FakeRequest("GET", "api.rabby.io", "/v1/user/addr?b=2&origin=chrome-extension://aaaa&a=1")
    r2 = FakeRequest("GET", "api.rabby.io", "/v1/user/addr?a=1&b=2&origin=chrome-extension://bbbb")
    r3 = FakeRequest("GET", "api.rabby.io", "/v1/user/addr?a=1&b=2")
    r4 = FakeRequest("GET", "api.rabby.io", "/v1/user/addr?a=1&b=2&origin=https://example.com")
    assert api_proxy.exact_key(r1) == api_proxy.exact_key(r2) == api_proxy.exact_key(r3)
    assert api_proxy.exact_key(r4) != api_proxy.exact_key(r3)


def test_non_rabby_hosts_and_drop_paths_are_killed(monkeypatch):
    blocked_host_req = FakeRequest("GET", "telemetry.example.com", "/v1/ping")
    dropped_path_req = FakeRequest("GET", "api.rabby.io", f"{api_proxy.DROP_PATHS[0]}?chain=eth")
    allowed_req = FakeRequest("GET", "static.debank.com", "/chain/list")
    entry = {"status": 200, "headers": {"content-type": "application/json"}, "text": "{}"}
    fx = _replay_fixture(
        monkeypatch,
        {
            api_proxy.exact_key(blocked_host_req): entry,
            api_proxy.exact_key(dropped_path_req): entry,
            api_proxy.exact_key(allowed_req): entry,
        },
    )

    f_host = FakeFlow(blocked_host_req)
    fx.request(f_host)
    assert f_host.killed and f_host.response is None

    f_drop = FakeFlow(dropped_path_req)
    fx.request(f_drop)
    assert f_drop.killed and f_drop.response is None

    f_ok = FakeFlow(allowed_req)
    fx.request(f_ok)
    assert not f_ok.killed and f_ok.response.status_code == 200


def test_replay_lookup_order_exact_then_loose_then_ref_then_miss(monkeypatch):
    req_exact = FakeRequest("POST", "api.rabby.io", "/v1/gas", b'{"chain":"eth"}')
    req_loose_direct = FakeRequest("POST", "api.rabby.io", "/v1/gas", b'{"chain":"arb"}')
    req_ref_orig = FakeRequest("POST", "api.rabby.io", "/v1/balance", b'{"nonce":1}')
    req_ref_variant = FakeRequest("POST", "api.rabby.io", "/v1/balance", b'{"nonce":2}')
    req_miss = FakeRequest("GET", "api.rabby.io", "/v1/unknown")

    raw_bytes = b"\x89PNG\x0d\x0a"
    fx = _replay_fixture(
        monkeypatch,
        {
            api_proxy.exact_key(req_exact): {
                "status": 200,
                "headers": {"content-type": "application/json"},
                "text": '{"hit":"exact"}',
            },
            api_proxy.loose_key(req_exact): {
                "status": 200,
                "headers": {"content-type": "application/json"},
                "text": '{"hit":"loose"}',
            },
            api_proxy.exact_key(req_ref_orig): {
                "status": 200,
                "headers": {"content-type": "application/octet-stream"},
                "b64": base64.b64encode(raw_bytes).decode(),
            },
            api_proxy.loose_key(req_ref_orig): {"ref": api_proxy.exact_key(req_ref_orig)},
        },
    )

    # 1. Exact match takes precedence over loose match.
    f1 = FakeFlow(req_exact)
    fx.request(f1)
    assert not f1.killed and f1.response.content == b'{"hit":"exact"}'

    # 2. Loose match serves when exact misses.
    f2 = FakeFlow(req_loose_direct)
    fx.request(f2)
    assert not f2.killed and f2.response.content == b'{"hit":"loose"}'

    # 3. Loose match with {"ref": exact_key} resolves the target entry (including b64 body).
    f3 = FakeFlow(req_ref_variant)
    fx.request(f3)
    assert not f3.killed and f3.response.content == raw_bytes

    # 4. Unmatched request kills the flow.
    f4 = FakeFlow(req_miss)
    fx.request(f4)
    assert f4.killed and f4.response is None
