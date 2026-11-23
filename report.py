"""Report building blocks shared by every flow: screen and frame diffs, pixel diffs, Markdown to a
self-contained HTML page."""

import base64
import difflib
import html
import json
import re
from pathlib import Path

# Device setup done by the driver. Pairing codes and the "Is now?" clock differ on every run, so
# these steps are left out of the diffs and only appear in the full filmstrips.
SETUP_STEPS = {"boot", "connect", "pairing", "seed"}


def images_equal(a, b):
    from PIL import Image, ImageChops
    return ImageChops.difference(Image.open(a).convert("RGB"), Image.open(b).convert("RGB")).getbbox() is None


def fmt_status(t):
    st = t.get("status")
    return "-" if st is None else ("pass" if st == "passed" else f"**{st.upper()}**")


def strip_ansi(s):
    return re.sub(r"\x1b\[[0-9;]*m", "", s)


def screen_lines(screens):
    """One line per screen, grouped by driver step. Bodies keep their newlines escaped."""
    lines = []
    for s in screens:
        if s["step"] in SETUP_STEPS:
            continue
        f = " | ".join(f"{k}={v!r}" for k, v in s["fields"].items())
        lines.append(f"[{s['step']}] {s['kind']}: {f}")
    return lines


def results(side_dir):
    p = side_dir / "headless" / "driver.jsonl"
    rows = {}
    if p.exists():
        for line in p.read_text().splitlines():
            ev = json.loads(line)
            if ev.get("event") == "step-end":
                rows[ev["step"]] = ("error: " + ev["error"]) if "error" in ev else "ok"
    return rows


def frame_rows(fb, fh, base_dir, head_dir, diffdir):
    """Two frame lists aligned by content hash, one (tag, base frame, head frame, pixel diff) per
    row; a side is None where its frame has no counterpart. Frames that replaced each other get a
    pixel diff in diffdir (None if the sizes differ)."""
    ops = difflib.SequenceMatcher(a=[f["hash"] for f in fb], b=[f["hash"] for f in fh], autojunk=False).get_opcodes()
    rows = []
    for tag, i1, i2, j1, j2 in ops:
        for k in range(max(i2 - i1, j2 - j1)):
            a = fb[i1 + k] if i1 + k < i2 else None
            b = fh[j1 + k] if j1 + k < j2 else None
            d = None
            if tag != "equal" and a and b:
                diffdir.mkdir(parents=True, exist_ok=True)
                d = pixel_diff(base_dir / a["file"], head_dir / b["file"],
                               diffdir / f"{Path(a['file']).stem}--{Path(b['file']).name}")
            rows.append((tag, a, b, d))
    return rows


def pixel_diff(a_path, b_path, out_path):
    from PIL import Image, ImageChops
    a, b = Image.open(a_path).convert("RGB"), Image.open(b_path).convert("RGB")
    if a.size != b.size:
        return None
    from PIL import ImageDraw, ImageFilter
    # Overlaying base and head pixels makes changed text unreadable. Instead draw head, dimmed,
    # with each changed line band re-drawn in red and boxed.
    mask = ImageChops.difference(a, b).convert("L").point(lambda v: 255 if v else 0)
    region = mask.filter(ImageFilter.MaxFilter(9))
    lum = b.convert("L")
    red = Image.merge("RGB", (lum, lum.point(lambda v: v // 5), lum.point(lambda v: v // 5)))
    out = Image.composite(red, b.point(lambda v: v // 3), region)
    draw, w = ImageDraw.Draw(out), out.width
    rows = [any(region.getpixel((x, y)) for x in range(w)) for y in range(out.height)] + [False]
    start = None
    for y, on in enumerate(rows):
        if on and start is None:
            start = y
        elif not on and start is not None:
            box = region.crop((0, start, w, y)).getbbox()
            draw.rectangle((box[0], start, box[2] - 1, y - 1), outline=(255, 60, 60))
            start = None
    out.save(out_path)
    return out_path



def frames_md(out, base_rel, head_rel, diff_rel):
    """Graphical frames that differ between two runs, aligned by content hash. Paths are relative
    to `out`, which is where report.md lives. Either side may be missing (new unit, failed run)."""
    gb, gh = out / base_rel, out / head_rel
    all_b, all_h = ([] if not (g / "frames.json").exists() else json.loads((g / "frames.json").read_text())
                    for g in (gb, gh))
    fb = [f for f in all_b if f["step"] not in SETUP_STEPS]
    fh = [f for f in all_h if f["step"] not in SETUP_STEPS]
    md = []
    rows = [r for r in frame_rows(fb, fh, gb, gh, out / diff_rel) if r[0] != "equal"]
    if not rows:
        md += [f"No change ({len(fb)} frames).", ""]
    else:
        md += ["| step | base | head | changed pixels |", "|---|---|---|---|"]
        for _, a, b, d in rows:
            step = (b or a)["step"]
            ca = f"![]({base_rel}/{a['file']})" if a else "*(absent)*"
            cb = f"![]({head_rel}/{b['file']})" if b else "*(absent)*"
            cd = f"![]({d.relative_to(out)})" if d else ""
            md.append(f"| `{step}` | {ca} | {cb} | {cd} |")
        md += ["", f"{len(rows)} differing frame(s); {len(fb)} base / {len(fh)} head frames total.", ""]
    for side, rel, frames in (("base", base_rel, all_b), ("head", head_rel, all_h)):
        if frames:
            md += [f"<details><summary>All {side} frames</summary>", ""]
            md += [" ".join(f"![{f['step']}]({rel}/{f['file']})" for f in frames), "", "</details>", ""]
    return md


def fmt_ok(v):
    return "skipped" if v is None else ("pass" if v else "**FAIL**")


def md_to_html(out, md):
    """Tiny renderer for the subset report() emits, with images inlined so the file stands alone.
    Report lines carry text from the code under test (unit ids, test names, error messages), so
    nothing in them may pick a file to read or reach the page as markup."""
    root = Path(out).resolve()

    def img(m):
        path = (root / html.unescape(m.group(2))).resolve()
        # Only the report's own images: `![x](/etc/passwd)` in a test name must not embed that file.
        if not path.is_relative_to(root) or path.suffix != ".png" or not path.is_file():
            return m.group(0)
        data = base64.b64encode(path.read_bytes()).decode()
        return f'<img title="{html.escape(html.unescape(m.group(1)))}" src="data:image/png;base64,{data}">'

    def inline(s):
        s = html.escape(s, quote=False)
        s = re.sub(r"!\[([^\]]*)\]\(([^)]+)\)", img, s)
        s = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", s)
        s = re.sub(r"\*([^*]+)\*", r"<i>\1</i>", s)
        return re.sub(r"`([^`]+)`", r"<code>\1</code>", s)

    body, i = [], 0
    while i < len(md):
        line = md[i]
        if line.startswith("```"):
            j = md.index("```", i + 1)
            rows = []
            for l in md[i + 1:j]:
                cls = {"+": "add", "-": "del", "@": "hunk"}.get(l[:1], "")
                rows.append(f'<span class="{cls}">{html.escape(l)}</span>')
            body.append("<pre>" + "\n".join(rows) + "</pre>")
            i = j + 1
            continue
        if line.startswith("|"):
            rows = []
            while i < len(md) and md[i].startswith("|"):
                if not re.match(r"^\|[-| ]+\|$", md[i]):
                    rows.append(md[i])
                i += 1
            cells = [[inline(c.strip()) for c in r.strip("|").split("|")] for r in rows]
            t = "<tr>" + "".join(f"<th>{c}</th>" for c in cells[0]) + "</tr>"
            t += "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in cells[1:])
            body.append(f"<table>{t}</table>")
            continue
        if line.startswith("- "):
            items = []
            while i < len(md) and md[i].startswith("- "):
                items.append(f"<li>{inline(md[i][2:])}</li>")
                i += 1
            body.append("<ul>" + "".join(items) + "</ul>")
            continue
        if line == "</details>":
            body.append(line)
        elif m := re.fullmatch(r"<details><summary>(.*)</summary>", line):
            body.append(f"<details><summary>{html.escape(m[1])}</summary>")
        elif line.startswith("<"):
            body.append(html.escape(line))
        elif line.startswith("### "):
            body.append(f"<h3>{inline(line[4:])}</h3>")
        elif line.startswith("## "):
            body.append(f"<h2>{inline(line[3:])}</h2>")
        elif line.startswith("# "):
            body.append(f"<h1>{inline(line[2:])}</h1>")
        elif line:
            body.append(f"<p>{inline(line)}</p>")
        i += 1
    css = ("body{font:14px system-ui,sans-serif;max-width:1100px;margin:2em auto;padding:0 1em}"
           "table{border-collapse:collapse;margin:1em 0}td,th{border:1px solid #ccc;padding:4px 8px;"
           "vertical-align:top}img{image-rendering:pixelated;margin:2px;border:1px solid #ddd}"
           "pre{background:#f6f8fa;padding:1em;overflow-x:auto}.add{color:#116329;background:#dafbe1}"
           ".del{color:#82071e;background:#ffebe9}.hunk{color:#8250df}")
    return f"<!doctype html><meta charset=utf-8><title>BitBox e2e diff</title><style>{css}</style>\n" + "\n".join(body)
