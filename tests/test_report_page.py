"""report.render_flow / render_run: the page and summary every flow writes. Their text comes from the
code under test, so it must not become markup, links or file reads."""

import base64

from bitbox_e2e import report
from bitbox_e2e.report import (
    CompareRow,
    FindingItem,
    Findings,
    ScreenChange,
    render_flow,
    render_run,
    write_flow_error,
)

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAAAAAA6fptVAAAACklEQVR4nGNgAAAAAgABSK+kcQAAAABJRU5ErkJggg=="
)


def screen(title, body="", step="sign", kind="CONFIRM"):
    return {"step": step, "kind": kind, "fields": {"TITLE": title, "BODY": body}}


def test_markup_in_findings_is_escaped(tmp_path):
    evil = "<script>alert(1)</script>"
    img_evil = "<img src=x onerror=alert(1)>"
    render_flow(
        tmp_path,
        Findings(
            title=evil,
            repo=f"BitBoxSwiss/{evil}",
            broke=[FindingItem(title=f"Test `{evil}`", detail=img_evil, excerpt=evil)],
        ),
    )
    page = (tmp_path / "report.html").read_text()
    assert "<script>" not in page and "<img src=x" not in page
    assert "&lt;script&gt;" in page and "&lt;img src=x" in page


def test_only_png_files_inside_the_report_are_shown_and_listed(tmp_path):
    out = tmp_path / "out"
    (out / "head").mkdir(parents=True)
    (out / "head" / "a.png").write_bytes(PNG)
    (out / "pr.json").write_text("{}")
    (tmp_path / "secret.png").write_bytes(PNG)
    rows = [
        CompareRow(before="../secret.png", after="head/a.png"),
        CompareRow(before="pr.json", after=str(tmp_path / "secret.png")),
    ]
    render_flow(out, Findings(title="t", changes=[ScreenChange(title="c", compare=rows)]))
    page = (out / "report.html").read_text()
    assert 'src="head/a.png"' in page and "secret" not in page and 'src="pr.json"' not in page
    assert (out / "report-files.txt").read_text().split() == ["report.html", "head/a.png"]


def test_summary_text_cannot_add_links_or_images():
    md = report.summary(
        Findings(
            title="t",
            broke=[FindingItem(title="Test ![x](https://evil/p.png) [y](https://evil)")],
        )
    )
    assert "](" not in md.replace("\\]", "")
    assert "`a_b*`" in report.summary(Findings(title="t", broke=[FindingItem(title="Test `a_b*`")]))


def test_removed_screen_is_named_in_the_change_title():
    base = [screen("Total"), screen("High fee", "Fee is 18%"), screen("Done")]
    head = [screen("Total"), screen("Done")]
    pairs = report.screen_changes(base, head)
    assert [(a and a["fields"]["TITLE"], b) for a, b in pairs] == [("High fee", None)]
    assert report.change_title(pairs) == 'Removed screen "High fee"'


def test_setup_steps_are_not_compared():
    assert (
        report.screen_changes(
            [screen("Pairing", "1234", step="pairing")],
            [screen("Pairing", "5678", step="pairing")],
        )
        == []
    )


def test_changes_of_the_same_kind_become_one_with_variants():
    a = ScreenChange(title="Changed transaction fee screen", where=["eth-matrix/1-eip1559"])
    b = ScreenChange(title="Changed transaction fee screen", where=["eth-matrix/56-eip1559"])
    c = ScreenChange(title='Removed screen "High fee"', where=["btc-vector/x"])
    merged = report.merge_similar([a, b, c])
    assert [(m.where, len(m.variants)) for m in merged] == [
        (["eth-matrix/1-eip1559", "eth-matrix/56-eip1559"], 1),
        (["btc-vector/x"], 0),
    ]


def test_render_run_aggregates_flows_rebases_images_and_is_idempotent(tmp_path):
    """Multi-flow run aggregation: device + wc + rabby flow-error.json all surface on the root page
    with rebased image paths, links to each sub-flow report, a complete report-files.txt, and
    identical output on a second render_run call."""
    (tmp_path / "device" / "head").mkdir(parents=True)
    (tmp_path / "device" / "head" / "fee.png").write_bytes(PNG)
    render_flow(
        tmp_path / "device",
        Findings(
            title="fw PR",
            repo="BitBoxSwiss/bitbox02-firmware",
            scope="2 device scenarios",
            changes=[
                ScreenChange(
                    title="Changed fee screen",
                    where=["btc-vector/fee"],
                    screens=[[screen("Fee", "10"), screen("Fee", "20")]],
                    compare=[CompareRow(label="Fee", before=None, after="head/fee.png")],
                )
            ],
        ),
    )

    (tmp_path / "wc" / "head").mkdir(parents=True)
    (tmp_path / "wc" / "head" / "s.png").write_bytes(PNG)
    render_flow(
        tmp_path / "wc",
        Findings(
            title="WalletConnect: firmware abc",
            scope="a dapp signs through the BitBoxApp",
            broke=[
                FindingItem(
                    title="WalletConnect request `eth_sendTransaction` fails",
                    compare=[CompareRow(label="App", before=None, after="head/s.png")],
                )
            ],
            changes=[
                ScreenChange(
                    title="Changed screen",
                    where=["personal-sign"],
                    screens=[[screen("A"), screen("B")]],
                )
            ],
        ),
    )

    write_flow_error(tmp_path / "rabby", "rabby", "Traceback\nRuntimeError: yarn install failed\n")

    render_run(tmp_path)
    page = (tmp_path / "report.html").read_text()
    summary = (tmp_path / "summary.md").read_text()
    files_text = (tmp_path / "report-files.txt").read_text()
    files = files_text.split()

    assert "Device tests: Changed fee screen" in page
    assert "WalletConnect: WalletConnect request <code>eth_sendTransaction</code> fails" in page
    assert "Rabby could not run" in page and "RuntimeError: yarn install failed" in page
    assert 'src="device/head/fee.png"' in page and 'src="wc/head/s.png"' in page
    assert '<a href="device/report.html">' in page and '<a href="wc/report.html">' in page
    assert "❌" in summary
    assert "WalletConnect: WalletConnect request" in summary
    assert "Device tests: Changed fee screen" in summary
    assert "Rabby could not run" in summary
    assert set(files) == {
        "report.html",
        "device/report.html",
        "device/head/fee.png",
        "wc/report.html",
        "wc/head/s.png",
    }
    assert len(files) == len(set(files))

    # A second render_run must be completely idempotent (no double-counting or duplicate files).
    render_run(tmp_path)
    assert (tmp_path / "report.html").read_text() == page
    assert (tmp_path / "summary.md").read_text() == summary
    assert (tmp_path / "report-files.txt").read_text() == files_text
