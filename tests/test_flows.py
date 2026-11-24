"""e2e.py's flow matrix: which repo is at which ref on each side of an integration flow."""

import e2e


def test_only_the_changed_repo_differs_between_sides(monkeypatch):
    monkeypatch.setattr(e2e, "flow_default", lambda r: {"firmware": "fwmaster", "app": "appmaster"}.get(r))
    s = e2e.flow_sides("app", "api-go", "gobase", "gohead", {"firmware": "fwpin"})
    assert s == {"changed": "api-go", "base": {"firmware": "fwpin", "app": "appmaster", "api-go": "gobase"},
                 "head": {"firmware": "fwpin", "app": "appmaster", "api-go": "gohead"}}


def test_each_integration_flow_builds_on_the_repo_that_changed():
    """Otherwise base and head would be the same build and the flow would test nothing."""
    for repo, flows in e2e.FLOWS.items():
        assert all(repo in e2e.FLOW_REPOS[f] for f in flows if f != "device"), repo


def test_an_app_that_no_longer_builds_against_the_change_is_a_regression(tmp_path):
    """An api-go change that breaks the app's compile: the tests cannot run, which must not read as
    'nothing broke'."""
    import json

    import wallet_app

    for side, err in (("base", None), ("head", "api/firmware/eth.go:12: undefined: ETHSignResult")):
        (tmp_path / side).mkdir()
        (tmp_path / side / "tests.json").write_text("[]" if err else json.dumps(
            [{"title": "send > works", "status": "passed", "error": "", "screenshot": None, "screens": []}]))
        (tmp_path / side / "meta.json").write_text(json.dumps(
            {"source": side, "simulator": "fw", "passed": 0 if err else 1, "total": 0 if err else 1,
             **({"build_error": err} if err else {})}))
    (tmp_path / "sides.json").write_text(json.dumps(
        {"changed": "api-go", "base": {"firmware": "f" * 40, "app": "a" * 40, "api-go": "b" * 40},
         "head": {"firmware": "f" * 40, "app": "a" * 40, "api-go": "c" * 40}}))
    assert wallet_app.wallet_report(tmp_path)
    summary = (tmp_path / "summary.md").read_text()
    assert "The BitBoxApp backend does not build" in summary
    assert "New tests" not in (tmp_path / "report.html").read_text()


def test_each_side_starts_with_a_fresh_app_but_keeps_downloaded_headers(tmp_path):
    """Both sides share the app clone when only the firmware differs; the app rebroadcasts the
    other side's transactions from eth-outgoing.db on startup, which changed the fees on the device."""
    import wallet_app

    data = tmp_path / "appfolder.dev"
    (data / "cache").mkdir(parents=True)
    for name in ("cache/eth-outgoing.db", "cache/headers-tbtc.bin", "config.json", "accounts.json"):
        (data / name).write_text("x")
    (data / "notes").mkdir()
    wallet_app.reset_app_data(tmp_path / "frontends" / "web")
    assert sorted(str(p.relative_to(data)) for p in data.rglob("*")) == ["cache", "cache/headers-tbtc.bin"]
