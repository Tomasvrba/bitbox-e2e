"""promote writes only into this checkout's scenarios/, whatever machine produced pr.json."""

import pytest

import e2e

SCENARIOS = (e2e.E2E_DIR / "scenarios").resolve()


def test_relative_paths_resolve_against_local_checkout():
    assert e2e.promote_path("scenarios/eth-erc20-unknown.json") == SCENARIOS / "eth-erc20-unknown.json"
    assert e2e.promote_path("scenarios/eth-matrix/1-legacy.json") == SCENARIOS / "eth-matrix" / "1-legacy.json"


def test_absolute_path_from_another_machine_is_refused():
    with pytest.raises(ValueError):
        e2e.promote_path("/home/runner/work/e2e/scenarios/x.json")


def test_escaping_scenarios_is_refused():
    with pytest.raises(ValueError):
        e2e.promote_path("scenarios/../e2e.py")
    with pytest.raises(ValueError):
        e2e.promote_path("../bitbox02-firmware/x.json")


def test_old_absolute_path_inside_local_scenarios_still_works():
    p = str(SCENARIOS / "x.json")
    assert e2e.promote_path(p) == SCENARIOS / "x.json"


def test_stored_unit_paths_round_trip():
    for u in e2e.bundle.load_units(e2e.E2E_DIR / "no-firmware-here"):
        if u.get("path"):
            assert e2e.promote_path(e2e.unit_path(u)) == e2e.Path(u["path"]).resolve()
