"""A suite that ran nothing must not count as passing."""

from bitbox_e2e import apis

RS_NONE = """running 0 tests

test result: ok. 0 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.00s
"""
RS_SOME = """running 2 tests
test eth::sign ... ok
test btc::sign ... FAILED
"""


def test_exit_zero_with_no_tests_is_an_error(tmp_path):
    res = apis.parse("api-rs", RS_NONE, tmp_path / "none.json")
    assert apis.suite_error(0, res)


def test_go_suite_with_no_simulator_tests_is_an_error(tmp_path):
    res = apis.parse("api-go", "testing: warning: no tests to run\nPASS\n", tmp_path / "none.json")
    assert apis.suite_error(0, res)


def test_vitest_without_results_file_is_an_error(tmp_path):
    res = apis.parse("api-ts", "", tmp_path / "missing.json")
    assert apis.suite_error(0, res)


def test_real_results_are_not_errors(tmp_path):
    res = apis.parse("api-rs", RS_SOME, tmp_path / "x.json")
    assert apis.suite_error(101, res) is None
    assert res["passing"] == ["eth::sign"] and res["failing"] == ["btc::sign"]


def test_nonzero_exit_without_failing_test_is_an_error(tmp_path):
    res = apis.parse("api-rs", "error[E0425]: cannot find value\n", tmp_path / "x.json")
    assert apis.suite_error(101, res)
