"""Request rules of the BitBoxApp's Etherscan proxy (etherscan-api.shiftcrypto.io), probed with
read-only calls on 2026-11-23. etherscan_fake.py enforces them; they are what the app's client can
get wrong (bitbox-wallet-app#4579)."""

MAX_GET_TARGET = 8147
CHAIN_HELP = "please see https://api.etherscan.io/v2/chainlist for the list of supported chainids"
MISSING = f"Missing chainid parameter (required for v2 api), {CHAIN_HELP}"
UNSUPPORTED = f"Missing or unsupported chainid parameter (required for v2 api), {CHAIN_HELP}"


def check(method, target, query, body, chains):
    """(chain id, where it was given: "url" | "body" | None, error or None) for one request.

    - The chain comes from the URL query (`chainId` or `chainid`). A POST that has it only in the
      body works for chain 1 and gets UNSUPPORTED for any other chain.
    - A GET whose path and query (target) are longer than MAX_GET_TARGET gets 414.
    """
    chain, given_in = query.get("chainId") or query.get("chainid"), "url"
    if not chain and method == "POST" and (chain := body.get("chainId") or body.get("chainid")):
        given_in = "body"
    if method == "GET" and len(target) > MAX_GET_TARGET:
        return chain, given_in if chain else None, "414 Request-URI Too Large"
    if not chain:
        return None, None, MISSING
    if (given_in == "body" and chain != "1") or chain not in chains:
        return chain, given_in, UNSUPPORTED
    return chain, given_in, None
