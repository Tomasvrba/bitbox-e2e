"""The report shared by every flow: screen and frame diffs, pixel diffs, and the page itself
(render: report.html and summary.md from one `findings` dict)."""

import difflib
import fnmatch
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



# --- the report page --------------------------------------------------------------------------------
#
# Every flow fills one `findings` dict; render() turns it into report.html (the product) and
# summary.md (its top, for the CI run page and the PR comment). Keys, all optional but title:
#
#   title, repo, pr ({url, number, title, body} or None), sides ({"base": str, "head": str}),
#   intent (intent(pr)), broke [item], fixed [str], changes [change], known [str], followups [str],
#   cautions [str], tested [str], untested [str], details [(label, base, head)], notes [str],
#   scope (str: what an integration flow ran, one line on the run's page),
#   incomplete [flow name] (set by with_flows: flows that could not run)
#   item:   {title, detail, excerpt, screens [(base screen, head screen)], compare [row], photo}
#   change: {title, where [id], expected True|False|None, screens [(base screen, head screen)],
#            compare [row], photo, notes [str]}   photo: screenshots, not device frames
#   row:    {label, before, after, marked, photo}: image paths relative to the report directory, or None
#
# Strings are plain text, often from the code under test (test names, errors, the PR text): they
# are escaped, only `backticks` become <code>.

def load_pr_info(out):
    p = Path(out) / "pr-info.json"
    return json.loads(p.read_text()) if p.exists() else None


def intent(pr_info):
    """The PR description's <!-- e2e ... --> block, one `key: value...` per line:
    {"expect": [globs], "graphical": [globs], "pins": [repo=ref]}."""
    m = re.search(r"<!--\s*e2e\b(.*?)-->", (pr_info or {}).get("body") or "", re.DOTALL)
    out = {}
    for line in (m[1] if m else "").splitlines():
        key, sep, val = line.split("#", 1)[0].partition(":")
        if sep and key.strip():
            out[key.strip().lower()] = val.split()
    return out


def is_expected(ids, globs):
    """Whether the PR lists one of ids as an expected change; None when it lists nothing."""
    if not globs:
        return None
    return any(fnmatch.fnmatchcase(i, g) for i in ids for g in globs)


def screen_changes(sb, sh):
    """Device screens that differ, outside setup steps: (base screen or None, head screen or None)."""
    sb, sh = ([s for s in x if s["step"] not in SETUP_STEPS] for x in (sb, sh))
    def key(s):
        return json.dumps([s["step"], s["kind"], s["fields"]], sort_keys=True)

    pairs = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(a=list(map(key, sb)), b=list(map(key, sh)),
                                                       autojunk=False).get_opcodes():
        if tag != "equal":
            pairs += [(sb[i1 + k] if i1 + k < i2 else None, sh[j1 + k] if j1 + k < j2 else None)
                      for k in range(max(i2 - i1, j2 - j1))]
    return pairs


def screen_name(s):
    """'screen "High fee"', or 'transaction fee screen' for a screen without a title."""
    title = s["fields"].get("TITLE") or s["fields"].get("title")
    if title:
        return f'screen "{" ".join(title.split())}"'
    return s["kind"].lower().removeprefix("confirm ") + " screen"


def change_title(pairs):
    """'Removed screen "High fee"' for the first two pairs of screen_changes."""
    parts = []
    for a, b in pairs[:2]:
        if a and b:
            na, nb = screen_name(a), screen_name(b)
            parts.append(f"Changed {nb}" if na == nb else f"{na[0].upper()}{na[1:]} became {nb}")
        else:
            parts.append(f"Removed {screen_name(a)}" if a else f"New {screen_name(b)}")
    more = len(pairs) - len(parts)
    return "; ".join(parts) + (f"; {more} more" if more > 0 else "") if parts else "Screens changed"


def merge_similar(changes):
    """Changes with the same title (the same kind of change, e.g. another fee amount per chain) as one
    change: the first one's evidence, the others as its variants."""
    out = {}
    for c in changes:
        key = (c["title"], c.get("expected"))
        if key in out:
            out[key]["variants"].append(c)
            out[key]["where"] = out[key]["where"] + (c.get("where") or [])
        else:
            out[key] = dict(c, where=list(c.get("where") or []), variants=[])
    return list(out.values())


def frame_compare(rows, out, base_rel, head_rel):
    """frame_rows output as compare rows: the frames that differ, with their pixel diff."""
    return [{"label": "Changed" if a and b else "Removed" if a else "Added",
             "before": f"{base_rel}/{a['file']}" if a else None,
             "after": f"{head_rel}/{b['file']}" if b else None,
             "marked": str(Path(d).relative_to(out)) if d else None}
            for tag, a, b, d in rows if tag != "equal"]


def plural(n, word):
    return f"{n} {word}" + ("" if n == 1 else "s")


# --- sides: which repo is at which ref before and after -------------------------------------------
#
# An integration flow (the app's tests, WalletConnect, Rabby) runs on top of several repos; the PR
# changes one of them. sides.json in the flow's directory: {"changed": repo, "base": {repo: ref},
# "head": {repo: ref}}; a ref of None means the version the app or wallet ships with.

REPO_NAMES = {"firmware": "firmware", "app": "BitBoxApp", "api-rs": "bitbox-api-rs", "api-go": "bitbox02-api-go",
              "api-ts": "bitbox-api-ts", "rabby": "Rabby"}
SHIPPED = {"api-go": "the version the app vendors", "api-ts": "the version Rabby's lockfile pins"}


def short_ref(ref):
    return ref[:9] if re.fullmatch(r"[0-9a-f]{40}", ref or "") else re.sub(r"^origin/", "", ref or "-")


def load_sides(out):
    p = Path(out) / "sides.json"
    return json.loads(p.read_text()) if p.exists() else None


def ref_text(repo, ref):
    return f"`{short_ref(ref)}`" if ref else SHIPPED.get(repo, "-")


def sides_header(sides):
    """findings["sides"]: the changed repo before and after."""
    c = sides["changed"]
    return {"base": f"{REPO_NAMES[c]} {ref_text(c, sides['base'][c])}, where the PR branched off",
            "head": f"{REPO_NAMES[c]} {ref_text(c, sides['head'][c])}"}


def sides_fixed(sides):
    """'BitBoxApp `abc` and bitbox02-api-go the version the app vendors': what both sides share."""
    parts = [f"{REPO_NAMES[r]} {ref_text(r, v)}" for r, v in sides["base"].items() if r != sides["changed"]]
    return ", ".join(parts[:-1]) + (" and " if len(parts) > 1 else "") + (parts[-1] if parts else "")


def sides_details(sides):
    """findings["details"] rows, one per repo."""
    return [(REPO_NAMES[r], short_ref(sides["base"][r]) if sides["base"][r] else "shipped",
             short_ref(sides["head"][r]) if sides["head"][r] else "shipped") for r in sides["base"]]


# --- integration flows: one page for the whole run -------------------------------------------------
#
# The flow of the changed repo writes its report into the run's directory; each integration flow
# writes its own into <run>/<flow>/ (findings.json, report.html, images). The run's page shows them
# all, each finding prefixed with its flow. A flow that could not run leaves flow-error.json.

FLOW_NAMES = {"device": "Device tests", "app": "BitBoxApp tests", "wc": "WalletConnect", "rabby": "Rabby"}
FLOW_DIRS = ("app", "wc", "rabby")


def write_flow_error(d, flow, error):
    Path(d).mkdir(parents=True, exist_ok=True)
    (Path(d) / "flow-error.json").write_text(json.dumps({"flow": flow, "error": error}))


def with_flows(out, f):
    """f with the findings of the integration flows in out/<flow>/ added."""
    out = Path(out)
    f = dict(f)
    for k in ("broke", "fixed", "changes", "minor", "known", "cautions", "tested"):
        f[k] = list(f.get(k) or [])
    globs = (f.get("intent") or {}).get("expect")
    for d in (out, *(out / x for x in FLOW_DIRS)):
        if (d / "flow-error.json").exists():
            e = json.loads((d / "flow-error.json").read_text())
            first = next((line for line in reversed(e["error"].splitlines()) if line.strip()), "no error message")
            f["cautions"].append(f"{FLOW_NAMES[e['flow']]} could not run, so it tested nothing: {first[:300]}")
            f["incomplete"] = [*(f.get("incomplete") or []), FLOW_NAMES[e["flow"]]]
    for flow in FLOW_DIRS:
        p = out / flow / "findings.json"
        if not p.exists():
            continue
        sub, name = json.loads(p.read_text()), FLOW_NAMES[flow]

        def rebase(rows, flow=flow):
            return [dict(r, **{k: f"{flow}/{r[k]}" for k in ("before", "after", "marked") if r.get(k)}) for r in rows or []]

        f["broke"] += [dict(b, title=f"{name}: {b['title']}", compare=rebase(b.get("compare"))) for b in sub.get("broke") or []]
        f["fixed"] += [f"{name}: {x}" for x in sub.get("fixed") or []]
        f["known"] += [f"{name}: {x}" for x in sub.get("known") or []]
        f["cautions"] += [f"{name}: {x}" for x in sub.get("cautions") or []]
        for c in sub.get("changes") or []:
            where = [f"{flow}/{w}" for w in c.get("where") or []]
            f["changes"].append(dict(c, title=f"{name}: {c['title']}", where=where, expected=is_expected(where, globs),
                                     compare=rebase(c.get("compare"))))
        f["minor"] += [dict(c, title=f"{name}: {c['title']}", compare=rebase(c.get("compare"))) for c in sub.get("minor") or []]
        top, rest = headline(sub)
        f["tested"].append(f"{name}: {sub.get('scope') or 'see its report'}. Result: {top}, {rest}. "
                           f"Its own report: {flow}/report.html")
    return f


def headline(f):
    broke, changes, incomplete = f.get("broke") or [], f.get("changes") or [], f.get("incomplete") or []
    top = (f"Broke {plural(len(broke), 'test')}" if broke else
           f"Incomplete: {' and '.join(incomplete)} could not run" if incomplete else "Nothing broke")
    if not changes:
        sub = "no device screen changed"
    else:
        exp = [c.get("expected") for c in changes]
        if None in exp:
            sub = f"{plural(len(changes), 'screen change')} to review"
        else:
            sub = f"{plural(exp.count(True), 'expected screen change')}"
            if exp.count(False):
                sub += f", {exp.count(False)} not listed as expected"
    return top, sub


CSS = """
:root{--bg:#f6f7f9;--card:#fff;--fg:#1b1f24;--muted:#5b6470;--line:#d9dde3;--code:#eef1f4;
--bad:#b42318;--bad-bg:#fdecea;--ok:#1a7f37;--ok-bg:#e8f5ec;--warn:#9a6700;--warn-bg:#fff6dd;--accent:#2f6feb}
@media (prefers-color-scheme:dark){:root{--bg:#0f1115;--card:#171a20;--fg:#e6e8eb;--muted:#9aa3ad;
--line:#2a2f37;--code:#232830;--bad:#ff7b72;--bad-bg:#3a1714;--ok:#56d364;--ok-bg:#12301b;
--warn:#e3b341;--warn-bg:#33290d;--accent:#6ea8fe}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,-apple-system,Segoe UI,sans-serif}
main{max-width:1100px;margin:0 auto;padding:24px 16px 64px}
a{color:var(--accent)}
code,pre{font:13px/1.45 ui-monospace,SFMono-Regular,Menlo,monospace;background:var(--code);border-radius:4px}
code{padding:1px 4px;overflow-wrap:anywhere}
pre{padding:10px 12px;overflow-x:auto;white-space:pre-wrap;overflow-wrap:anywhere;margin:8px 0}
.eyebrow{color:var(--muted);font-size:13px;letter-spacing:.04em;text-transform:uppercase}
h1{font-size:26px;line-height:1.25;margin:4px 0 6px}
h2{font-size:19px;margin:32px 0 10px}
h3{font-size:16px;margin:0}
.meta{color:var(--muted);font-size:14px}
.banner{margin:20px 0;padding:16px 18px;border-radius:8px;border:1px solid}
.banner .big{font-size:22px;font-weight:650}
.banner.fail{background:var(--bad-bg);border-color:var(--bad);color:var(--bad)}
.banner.pass{background:var(--ok-bg);border-color:var(--ok);color:var(--ok)}
.banner.warn{background:var(--warn-bg);border-color:var(--warn);color:var(--warn)}
.banner .sub{color:var(--fg)}
.callout{padding:10px 14px;border-left:4px solid var(--warn);background:var(--warn-bg);border-radius:4px;margin:10px 0}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:14px 16px;margin:12px 0}
.card.bad{border-left:4px solid var(--bad)}
.card-head{display:flex;gap:10px;align-items:baseline;justify-content:space-between;flex-wrap:wrap}
.tag{font-size:12px;font-weight:600;padding:2px 8px;border-radius:99px;white-space:nowrap}
.tag.yes{background:var(--ok-bg);color:var(--ok)}.tag.no{background:var(--bad-bg);color:var(--bad)}
.tag.review{background:var(--warn-bg);color:var(--warn)}
.muted,.where{color:var(--muted);font-size:14px}
.pairs{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin:10px 0}
.pairs .h{font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.04em}
.screen{background:var(--code);border-radius:6px;padding:8px 10px;white-space:pre-line;font-size:14px}
.screen .kind{font-size:11px;color:var(--muted);text-transform:uppercase}
.screen .t{font-weight:650}
.screen .k{color:var(--muted)}
.absent{color:var(--muted);font-style:italic;padding:8px 10px;border:1px dashed var(--line);border-radius:6px;text-align:center}
.cmp{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px;margin:10px 0;align-items:start}
.cmp .h{font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.04em}
.cmp.two{grid-template-columns:repeat(2,minmax(0,1fr))}
.cmp .label{grid-column:1/-1;font-weight:600;font-size:14px;margin-top:6px}
.cmp img{width:100%;height:auto;display:block;border-radius:4px;border:1px solid var(--line);image-rendering:pixelated}
.cmp img.photo{image-rendering:auto}
details{margin:10px 0}summary{cursor:pointer;font-weight:600}
details>div{padding:4px 0 0 16px}
table{border-collapse:collapse;font-size:14px;margin:8px 0;display:block;overflow-x:auto}
td,th{border:1px solid var(--line);padding:4px 8px;text-align:left;vertical-align:top}
ul{padding-left:20px}li{margin:3px 0}
.desc{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:10px 14px;white-space:pre-line;max-height:320px;overflow:auto}
@media (max-width:640px){.screen{font-size:12px;padding:6px}}
"""


def render(out, f):
    """Writes report.html, summary.md and report-files.txt (the page and every image it shows, for
    a publish step) into out. Images outside out, or not PNG files, are left out."""
    out = Path(out)
    root = out.resolve()
    shown = []

    def t(s):
        s = html.escape(str(s), quote=False)
        s = re.sub(r"\*\*([^*\n]+)\*\*", r"<b>\1</b>", s)
        s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
        return re.sub(r"\b((?:app|wc|rabby)/report\.html)\b", r'<a href="\1">\1</a>', s)

    def img(rel, alt, photo=False):
        if not rel:
            return '<div class="absent">none</div>'
        p = (root / rel).resolve()
        if not p.is_relative_to(root) or p.suffix != ".png" or not p.is_file():
            return '<div class="absent">image missing</div>'
        r = html.escape(str(p.relative_to(root)))
        shown.append(str(p.relative_to(root)))
        cls = ' class="photo"' if photo else ""
        return f'<a href="{r}" target="_blank"><img src="{r}" alt="{html.escape(alt)}"{cls} loading="lazy"></a>'

    def screen(s):
        if s is None:
            return '<div class="absent">no screen</div>'
        fields = dict(s["fields"])
        title = fields.pop("TITLE", None) or fields.pop("title", None)
        body = [f'<div class="kind">{t(s["kind"])}</div>']
        if title:
            body.append(f'<div class="t">{t(title)}</div>')
        body += [f'<div><span class="k">{t(k.lower())}:</span> {t(v)}</div>' for k, v in fields.items()]
        return '<div class="screen">' + "".join(body) + "</div>"

    def pairs(screens):
        h = ['<div class="pairs"><div class="h">Device screen before</div><div class="h">After</div>']
        for a, b in screens:
            h += [screen(a), screen(b)]
        return "".join(h) + "</div>"

    def compare(rows, photo=False):
        if not rows:
            return ""
        marked = any(r.get("marked") for r in rows)
        heads = ('<div class="h">Before</div><div class="h">After</div>'
                 + ('<div class="h">Change marked</div>' if marked else ""))
        h = [f'<div class="cmp{"" if marked else " two"}">']
        for i, r in enumerate(rows):
            if r.get("label"):
                h.append(f'<div class="label">{t(r["label"])}</div>')
            if i == 0:
                h.append(heads)
            ph = photo or r.get("photo", False)
            h += [img(r.get("before"), "before", ph), img(r.get("after"), "after", ph)]
            if marked:
                h.append(img(r["marked"], "marked", ph) if r.get("marked") else "<div></div>")
        return "".join(h) + "</div>"

    def excerpt(text):
        lines = text.splitlines()
        pre = f"<pre>{html.escape(text)}</pre>"
        return pre if len(lines) <= 12 else f"<details><summary>Error output ({len(lines)} lines)</summary>{pre}</details>"

    def ul(items):
        return "<ul>" + "".join(f"<li>{t(i)}</li>" for i in items) + "</ul>"

    (out / "findings.json").write_text(json.dumps(f, indent=1))
    f = with_flows(out, f)
    f = dict(f, changes=merge_similar(f.get("changes") or []))
    pr = f.get("pr")
    top, sub = headline(f)
    banner = "fail" if f.get("broke") else "warn" if f.get("incomplete") else "pass"
    h = ['<main><header>', f'<div class="eyebrow">BitBox e2e · {t(f.get("repo", ""))}</div>', f'<h1>{t(f["title"])}</h1>']
    meta = []
    if pr:
        meta.append(f'<a href="{html.escape(pr["url"])}">{t(pr.get("repo", ""))}#{t(pr.get("number", ""))}</a>')
    sides = f.get("sides") or {}
    if sides:
        meta.append(f'before: {t(sides["base"])} → after: {t(sides["head"])}')
    h += [f'<div class="meta">{" · ".join(meta)}</div>', "</header>"]
    known = f.get("known") or []
    extra = f" · {plural(len(known), 'problem')} already there before this PR" if known else ""
    h.append(f'<section class="banner {banner}"><div class="big">{t(top)}</div>'
             f'<div class="sub">{t(sub[0].upper() + sub[1:])}{t(extra)}</div></section>')
    h += [f'<div class="callout">{t(c)}</div>' for c in f.get("cautions") or []]

    h.append("<h2>What this PR should change</h2>")
    expect = (f.get("intent") or {}).get("expect")
    if pr:
        desc = re.sub(r"<!--.*?-->", "", pr.get("body") or "", flags=re.DOTALL).strip()
        h.append(f'<div class="desc">{t(desc) if desc else "<i>The PR has no description.</i>"}</div>')
    if expect:
        h.append(f'<p>Screen changes the PR expects: {", ".join(t(f"`{g}`") for g in expect)}. '
                 "Any other change is marked <b>not expected</b>.</p>")
    else:
        h.append('<p class="muted">The PR does not list the screen changes it expects, so every change below is '
                 "marked <b>to review</b>. To list them, add <code>&lt;!-- e2e expect: &lt;scenario glob&gt; --&gt;</code> "
                 "to the PR description.</p>")

    if f.get("broke"):
        h.append("<h2>What broke</h2><p class=muted>Each of these passed before this PR and fails with it.</p>")
        for b in f["broke"]:
            h.append(f'<article class="card bad"><h3>{t(b["title"])}</h3>')
            if b.get("detail"):
                h.append(f"<p>{t(b['detail'])}</p>")
            if b.get("excerpt"):
                h.append(excerpt(b["excerpt"]))
            if b.get("screens"):
                h.append(f"<details><summary>Device screens that differ: {len(b['screens'])}</summary>"
                         f"{pairs(b['screens'])}</details>")
            h.append(compare(b.get("compare"), b.get("photo", False)) + "</article>")

    if f.get("fixed"):
        h += ["<h2>Fixed by this PR</h2><p class=muted>Each of these failed before this PR and passes with it.</p>",
              ul(f["fixed"])]

    h.append("<h2>What changed on the device</h2>")
    if not f.get("changes"):
        h.append("<p>No device screen is different with this PR.</p>")
    for c in f.get("changes") or []:
        tag = {True: ("yes", "Expected"), False: ("no", "Not expected"), None: ("review", "To review")}[c.get("expected")]
        h.append(f'<article class="card"><div class="card-head"><h3>{t(c["title"])}</h3>'
                 f'<span class="tag {tag[0]}">{tag[1]}</span></div>')
        where = c.get("where") or []
        if where:
            more = f" and {len(where) - 1} more" if len(where) > 1 else ""
            h.append(f'<p class="where">In {t(f"`{where[0]}`")}{t(more)}.</p>')
            if len(where) > 1:
                h.append(f"<details><summary>All {len(where)}</summary><div>{ul(f'`{w}`' for w in where)}</div></details>")
        if c.get("screens"):
            h.append(pairs(c["screens"]))
        h.append(compare(c.get("compare"), c.get("photo", False)))
        h += [f'<p class="muted">{t(n)}</p>' for n in c.get("notes") or []]
        if c.get("variants"):
            h.append(f"<details><summary>The same kind of change with other values: {len(c['variants'])} more"
                     "</summary><div>")
            for v in c["variants"]:
                h.append(f'<p class="where">In {", ".join(t(f"`{w}`") for w in v.get("where") or [])}:</p>')
                if v.get("screens"):
                    h.append(pairs(v["screens"]))
                h.append(compare(v.get("compare"), v.get("photo", False)))
            h.append("</div></details>")
        h.append("</article>")

    if f.get("minor"):
        h.append(f"<details><summary>Pixel-only differences, same screen text: {len(f['minor'])} "
                 "(usually capture timing)</summary><div>")
        for c in f["minor"]:
            h.append(f'<article class="card"><h3>{t(c["title"])}</h3>{compare(c.get("compare"))}</article>')
        h.append("</div></details>")
    if f.get("followups"):
        h += ["<h2>To do after merge</h2>", ul(f["followups"])]
    h.append("<h2>What was tested</h2>" + ul(f.get("tested") or []))
    if f.get("untested"):
        h.append('<div class="callout"><b>Not tested by any scenario:</b>' + ul(f["untested"]) + "</div>")
    if known:
        h.append(f"<details><summary>Already failing before this PR: {len(known)} (not caused by it)</summary>"
                 f"<div>{ul(known)}</div></details>")
    if f.get("details") or f.get("notes"):
        rows = "".join(f"<tr><th>{t(k)}</th><td>{t(b)}</td><td>{t(a)}</td></tr>" for k, b, a in f.get("details") or [])
        h.append("<details><summary>Run details</summary><div>"
                 + (f"<table><tr><th></th><th>Before</th><th>After</th></tr>{rows}</table>" if rows else "")
                 + ul(f.get("notes") or []) + "</div></details>")
    h.append("</main>")
    title = html.escape(f"e2e: {f['title']}")
    page = (f'<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" '
            f'content="width=device-width,initial-scale=1"><title>{title}</title><style>{CSS}</style>\n'
            + "\n".join(h) + "\n</html>\n")
    (out / "report.html").write_text(page)
    (out / "summary.md").write_text(summary(f))
    sub_files = [f"{x}/{line}" for x in FLOW_DIRS if (out / x / "report-files.txt").exists()
                 for line in (out / x / "report-files.txt").read_text().split()]
    (out / "report-files.txt").write_text("\n".join(dict.fromkeys(["report.html", *shown, *sub_files])) + "\n")
    return out / "report.html"


def summary(f):
    """Markdown for the CI run page and the PR comment: the result and one line per finding."""
    def md(s):
        """Text from the code under test must not become links or markup in a comment; code spans
        stay as they are."""
        parts = re.split(r"(`[^`]*`)", str(s))
        return "".join(x if x.startswith("`") else re.sub(r"([\\\[\]<>!*_|#])", r"\\\1", x) for x in parts)

    f = dict(f, changes=merge_similar(f.get("changes") or []))
    top, sub = headline(f)
    icon = "❌" if f.get("broke") else "⚠️" if f.get("incomplete") else "✅"
    lines = [f"### {icon} {top} · {sub}", ""]
    if f.get("incomplete"):
        lines += [f"- {md(c)}" for c in f.get("cautions") or [] if "could not run" in c] + [""]
    if f.get("broke"):
        lines += ["**What broke**", ""] + [f"- {md(b['title'])}" for b in f["broke"]] + [""]
    if f.get("fixed"):
        lines += ["**Fixed by this PR**", ""] + [f"- {md(x)}" for x in f["fixed"]] + [""]
    if f.get("changes"):
        lines += ["**What changed on the device**", ""]
        for c in f["changes"]:
            tag = {True: "expected", False: "**not expected**", None: "to review"}[c.get("expected")]
            where = c.get("where") or []
            at = f" in `{where[0]}`" + (f" and {len(where) - 1} more" if len(where) > 1 else "") if where else ""
            lines.append(f"- {md(c['title'])}{md(at)} ({tag})")
        lines.append("")
    if f.get("known"):
        lines += [f"{plural(len(f['known']), 'problem')} already there before this PR (not caused by it): see the report.", ""]
    return "\n".join(lines)
