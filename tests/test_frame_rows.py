"""report.frame_rows: base and head frames aligned by content, pixel diffs only for replacements."""

from PIL import Image

from bitbox_e2e import report


def frames(tmp_path, side, *hashes):
    (tmp_path / side).mkdir()
    out = []
    for i, h in enumerate(hashes):
        name = f"{i:02d}-{h}.png"
        Image.new("RGB", (4, 4), (len(h) * 30, 0, 0)).save(tmp_path / side / name)
        out.append({"file": name, "step": "sign", "hash": h})
    return out


def test_replaced_frame_gets_a_pixel_diff_added_one_does_not(tmp_path):
    fb = frames(tmp_path, "base", "a", "b", "c")
    fh = frames(tmp_path, "head", "a", "bbb", "c", "dd")
    rows = report.frame_rows(fb, fh, tmp_path / "base", tmp_path / "head", tmp_path / "diff")
    got = [(tag, a and a["hash"], b and b["hash"], d is not None) for tag, a, b, d in rows]
    assert got == [
        ("equal", "a", "a", False),
        ("replace", "b", "bbb", True),
        ("equal", "c", "c", False),
        ("insert", None, "dd", False),
    ]
    assert rows[1][3].is_file()
