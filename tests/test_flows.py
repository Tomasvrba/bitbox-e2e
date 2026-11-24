"""Flow matrix, wallet backend build regressions, and app data cleanup."""

import json

from bitbox_e2e import cli
from bitbox_e2e.flows import wallet_app
from bitbox_e2e.report import Findings


def test_only_the_changed_repo_differs_between_sides(monkeypatch):
    monkeypatch.setattr(cli, "flow_default", lambda r: {"firmware": "fwmaster", "app": "appmaster"}.get(r))
    s = cli.flow_sides("app", "api-go", "gobase", "gohead", {"firmware": "fwpin"})
    assert s == {
        "changed": "api-go",
        "base": {"firmware": "fwpin", "app": "appmaster", "api-go": "gobase"},
        "head": {"firmware": "fwpin", "app": "appmaster", "api-go": "gohead"},
    }


def test_an_app_that_no_longer_builds_against_the_change_is_a_regression(tmp_path):
    """An api-go change that breaks the app's compile: the tests cannot run, which must not read as
    'nothing broke'."""
    err_msg = "api/firmware/eth.go:12: undefined: ETHSignResult"
    for side, err in (("base", None), ("head", err_msg)):
        (tmp_path / side).mkdir()
        tests_data = (
            []
            if err
            else [{"title": "send > works", "status": "passed", "error": "", "screenshot": None, "screens": []}]
        )
        (tmp_path / side / "tests.json").write_text(json.dumps(tests_data))
        meta = {
            "source": side,
            "simulator": "fw",
            "passed": 0 if err else 1,
            "total": 0 if err else 1,
            **({"build_error": err} if err else {}),
        }
        (tmp_path / side / "meta.json").write_text(json.dumps(meta))
    (tmp_path / "sides.json").write_text(
        json.dumps(
            {
                "changed": "api-go",
                "base": {"firmware": "f" * 40, "app": "a" * 40, "api-go": "b" * 40},
                "head": {"firmware": "f" * 40, "app": "a" * 40, "api-go": "c" * 40},
            }
        )
    )
    regressions = wallet_app.render(tmp_path)
    assert regressions == ["the backend does not build"]

    findings = Findings.from_dict(json.loads((tmp_path / "findings.json").read_text()))
    assert [b.title for b in findings.broke] == ["The BitBoxApp backend does not build"]
    assert findings.broke[0].excerpt == err_msg
    assert findings.changes == []
    assert findings.fixed == []
    # When a side fails to build, its empty test list must not be reported as added/removed tests.
    assert len(findings.tested) == 3

    summary = (tmp_path / "summary.md").read_text()
    assert "The BitBoxApp backend does not build" in summary


def test_each_side_starts_with_a_fresh_app_but_keeps_downloaded_headers(tmp_path):
    """Both sides share the app clone when only the firmware differs; the app rebroadcasts the
    other side's transactions from eth-outgoing.db on startup, which changed the fees on the device."""
    data = tmp_path / "appfolder.dev"
    (data / "cache").mkdir(parents=True)
    for name in ("cache/eth-outgoing.db", "cache/headers-tbtc.bin", "config.json", "accounts.json"):
        (data / name).write_text("x")
    (data / "notes").mkdir()
    wallet_app.reset_app_data(tmp_path / "frontends" / "web")
    assert sorted(str(p.relative_to(data)) for p in data.rglob("*")) == ["cache", "cache/headers-tbtc.bin"]
