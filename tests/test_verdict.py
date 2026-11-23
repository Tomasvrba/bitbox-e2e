"""The pr gate: only regressions against the merge-base fail it."""

import e2e


def unit(base_ok, head_ok, head_error=None):
    return {"base_ok": base_ok, "head_ok": head_ok, "head_error": head_error}


def suites(**by_suite):
    return {"simulator": "v", "suites": {s: {"firmware": r} for s, r in by_suite.items()}}


def suite(passing=(), failing=(), error=None):
    r = {"passed": len(passing), "failed": len(failing), "skipped": 0, "failing": list(failing),
         "passing": list(passing)}
    if error:
        r["error"] = error
    return r


CLEAN_APIS = suites(**{"api-rs": suite(passing=["a", "b"])})


def test_clean_run_passes():
    units = {"scenario/x": unit(True, True), "btc-vector/y": unit(False, False)}
    assert e2e.verdict(units, CLEAN_APIS, CLEAN_APIS) == []


def test_unit_ok_on_base_failing_on_head_fails_gate():
    reasons = e2e.verdict({"scenario/x": unit(True, False)}, CLEAN_APIS, CLEAN_APIS)
    assert len(reasons) == 1 and "scenario/x" in reasons[0]


def test_failure_reason_is_reported():
    reasons = e2e.verdict({"scenario/x": unit(True, False, "timeout")}, CLEAN_APIS, CLEAN_APIS)
    assert "timeout" in reasons[0]


def test_units_failing_on_both_sides_or_new_do_not_gate():
    units = {"btc-vector/err": unit(False, False), "scenario/new": unit(None, False)}
    assert e2e.verdict(units, CLEAN_APIS, CLEAN_APIS) == []


def test_api_test_passing_on_base_failing_on_head_fails_gate():
    head = suites(**{"api-rs": suite(passing=["a"], failing=["b"])})
    reasons = e2e.verdict({}, CLEAN_APIS, head)
    assert len(reasons) == 1 and "api-rs" in reasons[0] and "b" in reasons[0]


def test_api_test_failing_on_both_sides_does_not_gate():
    both = suites(**{"api-rs": suite(passing=["a"], failing=["b"])})
    assert e2e.verdict({}, both, both) == []


def test_api_test_not_run_on_base_does_not_gate():
    head = suites(**{"api-rs": suite(passing=["a", "b"], failing=["new"])})
    assert e2e.verdict({}, CLEAN_APIS, head) == []


def test_api_suite_error_on_head_fails_gate_even_if_base_errored():
    broken = suites(**{"api-ts": suite(error="build failed")})
    assert len(e2e.verdict({}, broken, broken)) == 1


def test_test_names_carrying_the_simulator_version_match_across_versions():
    # api-go and api-ts name tests after the simulator; a PR bumping the version renames them.
    old, new = "bitbox02-multi-v9.27.0-simulator1.0.0-linux-amd64", "bitbox02-multi-v9.28.0-simulator1.0.0-linux-amd64"
    base = {"simulator": old, "suites": {"api-go": {"firmware": suite(passing=[f"TestBTC/vec/{old}"])},
                                         "api-ts": {"firmware": suite(passing=["BTC '9.27.0' signs vectors"])}}}
    head = {"simulator": new, "suites": {"api-go": {"firmware": suite(failing=[f"TestBTC/vec/{new}"])},
                                         "api-ts": {"firmware": suite(failing=["BTC '9.28.0' signs vectors"])}}}
    reasons = e2e.verdict({}, base, head)
    assert len(reasons) == 2


def test_drift_against_committed_vector_copies_does_not_gate():
    # A PR that changes a screen fails the API repos' own vector copies until they are synced.
    base = {"simulator": "v", "suites": {"api-rs": {"committed": suite(passing=["a"]), "firmware": suite(passing=["a"])}}}
    head = {"simulator": "v", "suites": {"api-rs": {"committed": suite(failing=["a"]), "firmware": suite(passing=["a"])}}}
    assert e2e.verdict({}, base, head) == []
    assert e2e.api_newly_failing(base, head, "committed") == {"api-rs": ["a"]}


def test_mode_missing_on_base_does_not_gate():
    # A merge-base without the firmware's vectors file has no firmware-vector run to compare with.
    base = {"simulator": "v", "suites": {"api-rs": {"committed": suite(passing=["a"])}}}
    head = {"simulator": "v", "suites": {"api-rs": {"committed": suite(passing=["a"]),
                                                    "firmware": suite(passing=["a"], failing=["b"])}}}
    assert e2e.verdict({}, base, head) == []


def test_committed_vector_failures_gate_an_api_change_but_not_a_firmware_change():
    base = {"simulator": "v", "suites": {"api-go": {"committed": suite(passing=["t"])}}}
    head = {"simulator": "v", "suites": {"api-go": {"committed": suite(failing=["t"])}}}
    assert e2e.verdict({}, base, head) == []
    reasons = e2e.verdict({}, base, head, ("committed", "firmware"))
    assert len(reasons) == 1 and reasons[0].startswith("api-go (committed vectors)")


def test_vector_that_must_be_rejected_gates_only_when_head_accepts_it():
    rejected = dict(unit(False, False, "bitbox error: invalid input"), outcome="invalid_input")
    assert e2e.verdict({"btc-vector/r": rejected}, CLEAN_APIS, CLEAN_APIS) == []
    accepted = dict(unit(False, True), outcome="invalid_input")
    reasons = e2e.verdict({"btc-vector/r": accepted}, CLEAN_APIS, CLEAN_APIS)
    assert len(reasons) == 1 and "must be rejected" in reasons[0]
