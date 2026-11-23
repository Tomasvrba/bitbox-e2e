"""The local Etherscan proxy answers like the real one: the chain from the URL (#4579)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "wallet"))
import etherscan_rules as rules  # noqa: E402

CHAINS = {"1": 8645, "11155111": 8646}


def test_chain_in_the_url_works_for_get_and_post():
    assert rules.check("GET", "/v2/api?chainId=11155111&module=proxy", {"chainId": "11155111"}, {}, CHAINS)[2] is None
    assert rules.check("POST", "/v2/api?chainId=11155111", {"chainId": "11155111"}, {"module": "proxy"}, CHAINS)[2] is None


def test_chain_only_in_a_post_body_works_for_mainnet_only():
    assert rules.check("POST", "/v2/api", {}, {"chainId": "1"}, CHAINS) == ("1", "body", None)
    assert rules.check("POST", "/v2/api", {}, {"chainId": "11155111"}, CHAINS) == ("11155111", "body", rules.UNSUPPORTED)


def test_missing_or_unknown_chain_is_an_error():
    assert rules.check("GET", "/v2/api?module=proxy", {}, {}, CHAINS)[2] == rules.MISSING
    assert rules.check("GET", "/v2/api?chainId=999", {"chainId": "999"}, {}, CHAINS)[2] == rules.UNSUPPORTED


def test_too_long_get_is_414():
    target = "/v2/api?chainId=1&data=" + "0" * rules.MAX_GET_TARGET
    assert rules.check("GET", target, {"chainId": "1"}, {}, CHAINS)[2].startswith("414")
    assert rules.check("POST", target, {"chainId": "1"}, {}, CHAINS)[2] is None
