"""The HTML report embeds images and passes a little markup; report text comes from the code under test."""

import base64

import e2e

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAAAAAA6fptVAAAACklEQVR4nGNgAAAAAgABSK+kcQAAAABJRU5ErkJggg==")


def test_report_images_are_inlined(tmp_path):
    (tmp_path / "head").mkdir()
    (tmp_path / "head" / "a.png").write_bytes(PNG)
    page = e2e.md_to_html(tmp_path, ["![step](head/a.png)"])
    assert base64.b64encode(PNG).decode() in page


def test_files_outside_the_report_are_not_embedded(tmp_path):
    secret = tmp_path / "secret.png"
    secret.write_bytes(b"TOPSECRET")
    out = tmp_path / "out"
    out.mkdir()
    leaked = base64.b64encode(b"TOPSECRET").decode()
    for ref in (str(secret), "../secret.png"):
        assert leaked not in e2e.md_to_html(out, [f"### btc-vector/![x]({ref})"])


def test_only_png_files_are_embedded(tmp_path):
    (tmp_path / "pr.json").write_text("TOPSECRET")
    assert base64.b64encode(b"TOPSECRET").decode() not in e2e.md_to_html(tmp_path, ["![x](pr.json)"])


def test_markup_in_report_text_is_escaped(tmp_path):
    page = e2e.md_to_html(tmp_path, ["<details><summary>api-go: <script>alert(1)</script></summary>",
                                     "<img src=x onerror=alert(1)> summary", "</details>"])
    assert "<script>" not in page and "<img src=x" not in page
    assert "<details><summary>api-go: &lt;script&gt;" in page and "</details>" in page
