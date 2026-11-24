"""report.render: the page and summary every flow writes. Their text comes from the code under test
and the PR description, so it must not become markup, links or file reads."""

import base64

import report

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAAAAAA6fptVAAAACklEQVR4nGNgAAAAAgABSK+kcQAAAABJRU5ErkJggg==")


def screen(title, body="", step="sign", kind="CONFIRM"):
    return {"step": step, "kind": kind, "fields": {"TITLE": title, "BODY": body}}


def test_markup_in_findings_is_escaped(tmp_path):
    evil = "<script>alert(1)</script>"
    report.render(tmp_path, {"title": evil, "pr": {"url": "https://x/1", "number": 1, "body": f"<img src=x onerror=alert(1)> {evil}"},
                             "broke": [{"title": f"Test `{evil}`", "excerpt": evil}]})
    page = (tmp_path / "report.html").read_text()
    assert "<script>" not in page and "<img src=x" not in page
    assert "&lt;script&gt;" in page


def test_only_png_files_inside_the_report_are_shown_and_listed(tmp_path):
    out = tmp_path / "out"
    (out / "head").mkdir(parents=True)
    (out / "head" / "a.png").write_bytes(PNG)
    (out / "pr.json").write_text("{}")
    (tmp_path / "secret.png").write_bytes(PNG)
    rows = [{"before": "../secret.png", "after": "head/a.png"}, {"before": "pr.json", "after": str(tmp_path / "secret.png")}]
    report.render(out, {"title": "t", "changes": [{"title": "c", "compare": rows}]})
    page = (out / "report.html").read_text()
    assert 'src="head/a.png"' in page and "secret" not in page and 'src="pr.json"' not in page
    assert (out / "report-files.txt").read_text().split() == ["report.html", "head/a.png"]


def test_summary_text_cannot_add_links_or_images():
    md = report.summary({"title": "t", "broke": [{"title": "Test ![x](https://evil/p.png) [y](https://evil)"}]})
    assert "](" not in md.replace("\\]", "")
    assert "`a_b*`" in report.summary({"title": "t", "broke": [{"title": "Test `a_b*`"}]})


def test_intent_reads_the_e2e_block_of_the_pr_description():
    body = "Fix.\n<!-- e2e\nexpect: btc-vector/*fee*  eth-matrix/1-*   # the fee screens\ngraphical: x\n-->"
    assert report.intent({"body": body}) == {"expect": ["btc-vector/*fee*", "eth-matrix/1-*"], "graphical": ["x"]}
    assert report.intent({"body": "no block"}) == {} and report.intent(None) == {}


def test_expected_is_none_without_a_list_and_matches_globs_with_one():
    assert report.is_expected(["btc-vector/high-fee"], None) is None
    assert report.is_expected(["btc-vector/high-fee"], ["btc-vector/*fee*"]) is True
    assert report.is_expected(["eth-matrix/1-legacy"], ["btc-vector/*"]) is False


def test_removed_screen_is_named_in_the_change_title():
    base = [screen("Total"), screen("High fee", "Fee is 18%"), screen("Done")]
    head = [screen("Total"), screen("Done")]
    pairs = report.screen_changes(base, head)
    assert [(a and a["fields"]["TITLE"], b) for a, b in pairs] == [("High fee", None)]
    assert report.change_title(pairs) == 'Removed screen "High fee"'


def test_setup_steps_are_not_compared():
    assert report.screen_changes([screen("Pairing", "1234", step="pairing")], [screen("Pairing", "5678", step="pairing")]) == []


def test_changes_of_the_same_kind_become_one_with_variants():
    a = {"title": "Changed transaction fee screen", "where": ["eth-matrix/1-eip1559"]}
    b = {"title": "Changed transaction fee screen", "where": ["eth-matrix/56-eip1559"]}
    c = {"title": 'Removed screen "High fee"', "where": ["btc-vector/x"]}
    merged = report.merge_similar([a, b, c])
    assert [(m["where"], len(m["variants"])) for m in merged] == [(["eth-matrix/1-eip1559", "eth-matrix/56-eip1559"], 1),
                                                                   (["btc-vector/x"], 0)]


def test_integration_flows_show_on_the_runs_page(tmp_path):
    """A firmware PR that breaks WalletConnect: the run's page names the flow, its images resolve from
    the run's directory, its changes follow the PR's expect: globs, and a flow that could not run is
    said, not silently missing."""
    (tmp_path / "wc" / "head").mkdir(parents=True)
    (tmp_path / "wc" / "head" / "s.png").write_bytes(PNG)
    report.render(tmp_path / "wc", {
        "title": "WalletConnect: firmware abc", "scope": "a dapp signs through the BitBoxApp",
        "broke": [{"title": "WalletConnect request `eth_sendTransaction` fails",
                   "compare": [{"label": "App", "before": None, "after": "head/s.png"}]}],
        "changes": [{"title": "Changed screen", "where": ["personal-sign"], "screens": [(screen("A"), screen("B"))]}]})
    report.write_flow_error(tmp_path / "rabby", "rabby", "Traceback\nRuntimeError: yarn install failed\n")
    pr = {"url": "https://x/1", "number": 1, "body": "<!-- e2e\nexpect: wc/personal-*\n-->"}
    report.render(tmp_path, {"title": "fw PR", "pr": pr, "intent": report.intent(pr)})
    page = (tmp_path / "report.html").read_text()
    assert "WalletConnect: WalletConnect request <code>eth_sendTransaction</code> fails" in page
    assert 'src="wc/head/s.png"' in page
    assert "Expected" in page and "To review" not in page
    assert "Rabby could not run" in page
    assert '<a href="wc/report.html">' in page
    summary = (tmp_path / "summary.md").read_text()
    assert "❌" in summary and "WalletConnect: WalletConnect request" in summary
    files = (tmp_path / "report-files.txt").read_text().split()
    assert "wc/report.html" in files and "wc/head/s.png" in files
    # findings.json is the run's own flow only: a second render must not count WalletConnect twice.
    assert "WalletConnect request" not in (tmp_path / "findings.json").read_text()
