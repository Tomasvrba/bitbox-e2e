"""The report shared by every flow: typed Findings dataclasses, screen and frame diffs, pixel diffs,
per-flow rendering (render_flow), and uniform run aggregation (render_run).
"""

from __future__ import annotations

import difflib
import html
import json
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from urllib.parse import quote

from bitbox_e2e.config import FLOW_NAMES, FLOW_ORDER, REPO_NAMES, SHIPPED

# Device setup done by the driver. Pairing codes and the "Is now?" clock differ on every run, so
# these steps are left out of the diffs and only appear in the full filmstrips.
SETUP_STEPS = {"boot", "connect", "pairing", "seed"}


# --- typed report model ---------------------------------------------------------------------------


@dataclass
class CompareRow:
    label: str = ""
    before: str | None = None
    after: str | None = None
    marked: str | None = None
    photo: bool = False

    def to_dict(self) -> dict:
        return {
            "label": self.label,
            "before": self.before,
            "after": self.after,
            "marked": self.marked,
            "photo": self.photo,
        }

    @classmethod
    def from_dict(cls, d: dict) -> CompareRow:
        if isinstance(d, cls):
            return d
        return cls(
            label=d.get("label") or "",
            before=d.get("before"),
            after=d.get("after"),
            marked=d.get("marked"),
            photo=bool(d.get("photo", False)),
        )

    def rebase(self, prefix: str) -> CompareRow:
        return CompareRow(
            label=self.label,
            before=f"{prefix}/{self.before}" if self.before else None,
            after=f"{prefix}/{self.after}" if self.after else None,
            marked=f"{prefix}/{self.marked}" if self.marked else None,
            photo=self.photo,
        )


@dataclass
class FindingItem:
    title: str
    detail: str = ""
    excerpt: str | None = None
    screens: list[list[dict | None]] = field(default_factory=list)
    compare: list[CompareRow] = field(default_factory=list)
    photo: bool = False

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "detail": self.detail,
            "excerpt": self.excerpt,
            "screens": [list(pair) for pair in self.screens],
            "compare": [r.to_dict() for r in self.compare],
            "photo": self.photo,
        }

    @classmethod
    def from_dict(cls, d: dict) -> FindingItem:
        if isinstance(d, cls):
            return d
        return cls(
            title=d["title"],
            detail=d.get("detail") or "",
            excerpt=d.get("excerpt"),
            screens=[list(pair) for pair in d.get("screens") or []],
            compare=[CompareRow.from_dict(r) for r in d.get("compare") or []],
            photo=bool(d.get("photo", False)),
        )


@dataclass
class ScreenChange:
    title: str
    where: list[str] = field(default_factory=list)
    screens: list[list[dict | None]] = field(default_factory=list)
    compare: list[CompareRow] = field(default_factory=list)
    photo: bool = False
    notes: list[str] = field(default_factory=list)
    variants: list[ScreenChange] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "where": list(self.where),
            "screens": [list(pair) for pair in self.screens],
            "compare": [r.to_dict() for r in self.compare],
            "photo": self.photo,
            "notes": list(self.notes),
            "variants": [v.to_dict() for v in self.variants],
        }

    @classmethod
    def from_dict(cls, d: dict) -> ScreenChange:
        if isinstance(d, cls):
            return d
        return cls(
            title=d["title"],
            where=list(d.get("where") or []),
            screens=[list(pair) for pair in d.get("screens") or []],
            compare=[CompareRow.from_dict(r) for r in d.get("compare") or []],
            photo=bool(d.get("photo", False)),
            notes=list(d.get("notes") or []),
            variants=[cls.from_dict(v) for v in d.get("variants") or []],
        )


@dataclass
class Findings:
    title: str
    repo: str = ""
    sides: dict[str, str] = field(default_factory=dict)
    scope: str = ""
    broke: list[FindingItem] = field(default_factory=list)
    fixed: list[str] = field(default_factory=list)
    changes: list[ScreenChange] = field(default_factory=list)
    minor: list[ScreenChange] = field(default_factory=list)
    proof: list[ScreenChange] = field(default_factory=list)
    known: list[str] = field(default_factory=list)
    followups: list[str] = field(default_factory=list)
    cautions: list[str] = field(default_factory=list)
    tested: list[str] = field(default_factory=list)
    details: list[tuple[str, str, str]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    incomplete: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "repo": self.repo,
            "sides": dict(self.sides),
            "scope": self.scope,
            "broke": [b.to_dict() for b in self.broke],
            "fixed": list(self.fixed),
            "changes": [c.to_dict() for c in self.changes],
            "minor": [m.to_dict() for m in self.minor],
            "proof": [p.to_dict() for p in self.proof],
            "known": list(self.known),
            "followups": list(self.followups),
            "cautions": list(self.cautions),
            "tested": list(self.tested),
            "details": [list(row) for row in self.details],
            "notes": list(self.notes),
            "incomplete": list(self.incomplete),
        }

    @classmethod
    def from_dict(cls, d: dict) -> Findings:
        if isinstance(d, cls):
            return d
        return cls(
            title=d["title"],
            repo=d.get("repo") or "",
            sides=dict(d.get("sides") or {}),
            scope=d.get("scope") or "",
            broke=[FindingItem.from_dict(b) for b in d.get("broke") or []],
            fixed=list(d.get("fixed") or []),
            changes=[ScreenChange.from_dict(c) for c in d.get("changes") or []],
            minor=[ScreenChange.from_dict(m) for m in d.get("minor") or []],
            proof=[ScreenChange.from_dict(p) for p in d.get("proof") or []],
            known=list(d.get("known") or []),
            followups=list(d.get("followups") or []),
            cautions=list(d.get("cautions") or []),
            tested=list(d.get("tested") or []),
            details=[(str(r[0]), str(r[1]), str(r[2])) for r in d.get("details") or []],
            notes=list(d.get("notes") or []),
            incomplete=list(d.get("incomplete") or []),
        )


# --- image & screen diff helpers ------------------------------------------------------------------


def images_equal(a, b) -> bool:
    from PIL import Image, ImageChops

    return ImageChops.difference(Image.open(a).convert("RGB"), Image.open(b).convert("RGB")).getbbox() is None


def strip_ansi(s: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", s)


def screen_lines(screens: list[dict]) -> list[str]:
    """One line per screen, grouped by driver step. Bodies keep their newlines escaped."""
    lines = []
    for s in screens:
        if s["step"] in SETUP_STEPS:
            continue
        fields = " | ".join(f"{k}={v!r}" for k, v in s["fields"].items())
        lines.append(f"[{s['step']}] {s['kind']}: {fields}")
    return lines


def results(side_dir: Path) -> dict[str, str]:
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
                d = pixel_diff(
                    base_dir / a["file"],
                    head_dir / b["file"],
                    diffdir / f"{Path(a['file']).stem}--{Path(b['file']).name}",
                )
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


def screen_changes(sb: list[dict], sh: list[dict]) -> list[list[dict | None]]:
    """Device screens that differ, outside setup steps: [base screen or None, head screen or None]."""
    sb = [s for s in sb if s["step"] not in SETUP_STEPS]
    sh = [s for s in sh if s["step"] not in SETUP_STEPS]

    def key(s):
        return json.dumps([s["step"], s["kind"], s["fields"]], sort_keys=True)

    pairs: list[list[dict | None]] = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(
        a=list(map(key, sb)),
        b=list(map(key, sh)),
        autojunk=False,
    ).get_opcodes():
        if tag != "equal":
            pairs += [
                [sb[i1 + k] if i1 + k < i2 else None, sh[j1 + k] if j1 + k < j2 else None]
                for k in range(max(i2 - i1, j2 - j1))
            ]
    return pairs


def screen_name(s: dict) -> str:
    """'screen "High fee"', or 'transaction fee screen' for a screen without a title."""
    title = s["fields"].get("TITLE") or s["fields"].get("title")
    if title:
        return f'screen "{" ".join(title.split())}"'
    return s["kind"].lower().removeprefix("confirm ") + " screen"


def change_title(pairs: list[list[dict | None]]) -> str:
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


def merge_similar(changes: list[ScreenChange]) -> list[ScreenChange]:
    """Changes with the same title (the same kind of change, e.g. another fee amount per chain) as
    one change: the first one's evidence, the others as its variants."""
    out: dict[str, ScreenChange] = {}
    for c in changes:
        if c.title in out:
            base = out[c.title]
            base.variants.append(c)
            base.where.extend(c.where)
        else:
            out[c.title] = ScreenChange(
                title=c.title,
                where=list(c.where),
                screens=list(c.screens),
                compare=list(c.compare),
                photo=c.photo,
                notes=list(c.notes),
                variants=list(c.variants),
            )
    return list(out.values())


def frame_compare(rows, out, base_rel, head_rel) -> list[CompareRow]:
    """frame_rows output as compare rows: the frames that differ, with their pixel diff."""
    return [
        CompareRow(
            label="Changed" if a and b else "Removed" if a else "Added",
            before=f"{base_rel}/{a['file']}" if a else None,
            after=f"{head_rel}/{b['file']}" if b else None,
            marked=str(Path(d).relative_to(out)) if d else None,
        )
        for tag, a, b, d in rows
        if tag != "equal"
    ]


def plural(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


# --- sides: which repo is at which ref before and after -------------------------------------------


def short_ref(ref: str | None) -> str:
    return ref[:9] if re.fullmatch(r"[0-9a-f]{40}", ref or "") else re.sub(r"^origin/", "", ref or "-")


def load_sides(out: Path | str) -> dict | None:
    p = Path(out) / "sides.json"
    return json.loads(p.read_text()) if p.exists() else None


def ref_text(repo: str, ref: str | None) -> str:
    return f"`{short_ref(ref)}`" if ref else SHIPPED.get(repo, "-")


def sides_header(sides: dict) -> dict[str, str]:
    """Findings.sides: the changed repo before and after."""
    c = sides["changed"]
    return {
        "base": f"{REPO_NAMES[c]} {ref_text(c, sides['base'][c])}, where the PR branched off",
        "head": f"{REPO_NAMES[c]} {ref_text(c, sides['head'][c])}",
    }


def sides_fixed(sides: dict) -> str:
    """'BitBoxApp `abc` and bitbox02-api-go the version the app vendors': what both sides share."""
    parts = [f"{REPO_NAMES[r]} {ref_text(r, v)}" for r, v in sides["base"].items() if r != sides["changed"]]
    return ", ".join(parts[:-1]) + (" and " if len(parts) > 1 else "") + (parts[-1] if parts else "")


def sides_details(sides: dict) -> list[tuple[str, str, str]]:
    """Findings.details rows, one per repo."""
    return [
        (
            REPO_NAMES[r],
            short_ref(sides["base"][r]) if sides["base"][r] else "shipped",
            short_ref(sides["head"][r]) if sides["head"][r] else "shipped",
        )
        for r in sides["base"]
    ]


# --- flow errors & aggregation --------------------------------------------------------------------


def write_flow_error(flow_dir: Path, flow: str, error: str) -> None:
    flow_dir = Path(flow_dir)
    flow_dir.mkdir(parents=True, exist_ok=True)
    (flow_dir / "flow-error.json").write_text(json.dumps({"flow": flow, "error": error}))


def _rebase_change(c: ScreenChange, flow: str, prefix_title: str | None = None) -> ScreenChange:
    return ScreenChange(
        title=f"{prefix_title}: {c.title}" if prefix_title else c.title,
        where=[f"{flow}/{w}" for w in c.where],
        screens=list(c.screens),
        compare=[r.rebase(flow) for r in c.compare],
        photo=c.photo,
        notes=list(c.notes),
        variants=[_rebase_change(v, flow) for v in c.variants],
    )


def headline(f: Findings) -> tuple[str, str]:
    if f.broke:
        top = f"Broke {plural(len(f.broke), 'test')}"
        sub = f"{plural(len(f.changes), 'screen change')} to review" if f.changes else "no device screen changed"
    elif f.incomplete:
        top = f"Incomplete: {' and '.join(f.incomplete)} could not run"
        sub = f"{plural(len(f.changes), 'screen change')} to review" if f.changes else "no device screen changed"
    elif f.changes:
        top = f"Review {plural(len(f.changes), 'screen change')}"
        sub = "nothing broke"
    else:
        top = "Nothing broke"
        sub = "no device screen changed"
    return top, sub


# --- HTML & Markdown rendering --------------------------------------------------------------------


CSS = """
:root{
--bg:#111214;--surface:#191b1f;--surface-2:#22252b;--line:#2e323a;--line-strong:#3f4550;
--fg:#f2f3f5;--fg-2:#c7ccd4;--muted:#8d95a2;--code:#14161a;
--accent:#4b96ff;--accent-bg:rgba(75,150,255,.14);
--ok:#36b37e;--ok-bg:rgba(54,179,126,.14);--ok-fg:#7ce0b2;
--bad:#f06a6a;--bad-bg:rgba(240,106,106,.14);--bad-fg:#ff9d9d;
--warn:#e3a936;--warn-bg:rgba(227,169,54,.15);--warn-fg:#f6ce74;
--mono:ui-monospace,SFMono-Regular,"JetBrains Mono",Menlo,monospace
}
@media (prefers-color-scheme:light){:root{
--bg:#f4f5f7;--surface:#ffffff;--surface-2:#f8f9fb;--line:#dfe3ea;--line-strong:#c5ccd7;
--fg:#15181d;--fg-2:#373f4a;--muted:#5c6574;--code:#eef1f5;
--accent:#1f6feb;--accent-bg:rgba(31,111,235,.10);
--ok:#1a7f37;--ok-bg:rgba(26,127,55,.10);--ok-fg:#14682c;
--bad:#c62828;--bad-bg:rgba(198,40,40,.09);--bad-fg:#a51d1d;
--warn:#9a6700;--warn-bg:rgba(154,103,0,.11);--warn-fg:#7d5300
}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
font:15px/1.55 Inter,system-ui,-apple-system,Segoe UI,sans-serif;-webkit-font-smoothing:antialiased}
main{max-width:1160px;margin:0 auto;padding:36px 22px 72px}
a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
code,pre{font:13px/1.5 var(--mono);background:var(--code);border-radius:6px}
code{padding:2px 6px;overflow-wrap:anywhere;border:1px solid var(--line)}
pre{padding:12px 14px;overflow-x:auto;white-space:pre-wrap;overflow-wrap:anywhere;margin:10px 0;
border:1px solid var(--line);color:var(--fg-2)}

/* Header & hero */
header{margin-bottom:22px}
.eyebrow{color:var(--accent);font-size:12px;letter-spacing:.12em;text-transform:uppercase;font-weight:700}
h1{font-size:30px;line-height:1.18;letter-spacing:-.02em;margin:6px 0 10px}
.sides-bar{display:flex;flex-wrap:wrap;gap:8px;align-items:center;color:var(--muted);font-size:13px}
.sides-bar .arrow{color:var(--accent);font-weight:700}
.side-chip{background:var(--surface);border:1px solid var(--line);border-radius:99px;padding:4px 11px;color:var(--fg-2)}
.side-chip b{color:var(--fg);font-weight:600}

.hero{margin:18px 0 22px;padding:20px 22px;border-radius:14px;border:1px solid;
display:flex;flex-direction:column;gap:14px}
.hero.fail{background:var(--bad-bg);border-color:var(--bad)}
.hero.pass{background:var(--ok-bg);border-color:var(--ok)}
.hero.warn{background:var(--warn-bg);border-color:var(--warn)}
.hero-top{display:flex;gap:14px;align-items:baseline;justify-content:space-between;flex-wrap:wrap}
.hero .big{font-size:24px;font-weight:750;letter-spacing:-.015em}
.hero.fail .big{color:var(--bad-fg)}.hero.pass .big{color:var(--ok-fg)}.hero.warn .big{color:var(--warn-fg)}
.hero .sub{color:var(--fg-2);font-size:15px}

/* Flow scorecard */
.scorecard{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:10px}
.flow-tile{background:var(--surface);border:1px solid var(--line);border-radius:10px;
padding:12px 14px;display:flex;flex-direction:column;gap:6px}
.flow-tile.fail{border-color:var(--bad)}.flow-tile.warn{border-color:var(--warn)}
.flow-tile.changed{border-color:var(--accent)}.flow-tile.pass{border-color:var(--ok)}
.flow-tile-head{display:flex;align-items:center;justify-content:space-between;gap:8px}
.flow-tile-name{font-weight:700;font-size:14px;color:var(--fg)}
.flow-tile-sub{font-size:13px;color:var(--muted);line-height:1.35;flex:1}
.flow-tile-foot{font-size:12px;margin-top:2px}

/* Pills & section headers */
.pill{font:600 11.5px/1 var(--mono);padding:4px 9px;border-radius:99px;display:inline-flex;align-items:center;
letter-spacing:.02em;white-space:nowrap}
.pill.bad{background:var(--bad-bg);color:var(--bad-fg);border:1px solid var(--bad)}
.pill.good{background:var(--ok-bg);color:var(--ok-fg);border:1px solid var(--ok)}
.pill.warn{background:var(--warn-bg);color:var(--warn-fg);border:1px solid var(--warn)}
.pill.acc{background:var(--accent-bg);color:var(--accent);border:1px solid var(--accent)}
.pill.muted{background:var(--surface-2);color:var(--muted);border:1px solid var(--line)}

h2{font-size:20px;letter-spacing:-.01em;margin:34px 0 6px;display:flex;align-items:center;gap:10px}
h3{font-size:17px;line-height:1.35;margin:0;letter-spacing:-.01em}
.sec-sub{color:var(--muted);font-size:14px;margin:0 0 14px}
.callout{padding:11px 15px;border-left:4px solid var(--warn);background:var(--warn-bg);
border-radius:8px;margin:10px 0;color:var(--fg)}

/* Finding cards */
.card{background:var(--surface);border:1px solid var(--line);border-radius:12px;padding:18px 20px;margin:14px 0}
.card.bad{border-left:4px solid var(--bad)}
.card.chg{border-left:4px solid var(--accent)}
.card-head{display:flex;gap:10px;align-items:center;justify-content:space-between;flex-wrap:wrap;margin-bottom:6px}
.card-title-row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.card-detail{color:var(--fg-2);margin:6px 0 10px;font-size:14.5px}
.muted,.where{color:var(--muted);font-size:13.5px;margin:4px 0 8px}

/* Screen text diffs */
.pairs{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin:12px 0}
.pairs .h{font:700 11px/1 var(--mono);color:var(--muted);text-transform:uppercase;letter-spacing:.08em}
.screen{background:var(--code);border:1px solid var(--line);border-radius:8px;padding:10px 12px;
white-space:pre-line;font-size:13.5px}
.screen.before{border-left:3px solid var(--bad)}.screen.after{border-left:3px solid var(--ok)}
.screen .kind{font:600 11px/1 var(--mono);color:var(--muted);text-transform:uppercase;margin-bottom:4px}
.screen .t{font-weight:700;color:var(--fg);margin-bottom:2px}
.screen .k{color:var(--muted)}
.absent{color:var(--muted);font-style:italic;padding:14px 10px;border:1px dashed var(--line-strong);
border-radius:8px;text-align:center;font-size:13px}

/* Visual comparison grids */
.cmp{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px;margin:12px 0;align-items:start}
.cmp.two{grid-template-columns:repeat(2,minmax(0,1fr))}
.cmp .h{font:700 11px/1 var(--mono);color:var(--muted);text-transform:uppercase;letter-spacing:.08em}
.cmp .label{grid-column:1/-1;font-weight:650;font-size:13.5px;color:var(--fg-2);margin-top:6px}
.cmp img{width:100%;height:auto;display:block;border-radius:8px;border:2px solid var(--line-strong);
image-rendering:pixelated;background:#000}
.cmp img.photo{image-rendering:auto;border-width:1px;background:var(--code)}
.cmp img.after-ok{border-color:var(--ok)}.cmp img.diff-hit{border-color:var(--bad)}

/* Passing proof filmstrip */
.filmstrip{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:12px;
margin:12px 0;align-items:start}
.filmstrip figure{margin:0;display:flex;flex-direction:column;gap:6px;background:var(--surface-2);
border:1px solid var(--line);border-radius:9px;padding:8px}
.filmstrip figcaption{font-size:12.5px;color:var(--fg-2);overflow-wrap:anywhere}
.filmstrip img{width:100%;height:auto;display:block;border-radius:6px;border:1px solid var(--line-strong);
image-rendering:pixelated;background:#000}
.filmstrip img.photo{image-rendering:auto;background:var(--code)}

/* Collapsible reference drawers */
details{margin:10px 0;background:var(--surface-2);border:1px solid var(--line);border-radius:10px;padding:8px 14px}
details[open]{padding-bottom:12px}
summary{cursor:pointer;font-weight:600;font-size:14px;color:var(--fg);list-style:none;
display:flex;align-items:center;gap:8px}
summary::-webkit-details-marker{display:none}
summary::before{content:"▸";color:var(--accent);font-size:12px;transition:transform .15s}
details[open]>summary::before{transform:rotate(90deg)}
details>div{padding:8px 0 0}
.card details{background:var(--code);margin:10px 0 0}
.ref-section{margin-top:34px;padding-top:18px;border-top:1px solid var(--line)}

table{border-collapse:collapse;font-size:13.5px;margin:8px 0;width:100%;display:block;overflow-x:auto}
td,th{border:1px solid var(--line);padding:6px 10px;text-align:left;vertical-align:top}
th{background:var(--surface);color:var(--fg);font-weight:650}
ul{padding-left:20px;margin:6px 0}li{margin:4px 0;color:var(--fg-2)}
@media (max-width:680px){
  main{padding:20px 14px 48px}
  h1{font-size:23px}
  .pairs,.cmp,.cmp.two{grid-template-columns:1fr}
  .screen{font-size:12.5px;padding:8px}
}
"""


def _split_flow_prefix(title: str) -> tuple[str | None, str]:
    """Split 'WalletConnect: ...' into ('WalletConnect', '...') when the prefix is a known flow name."""
    for name in FLOW_NAMES.values():
        prefix = f"{name}: "
        if title.startswith(prefix):
            return name, title[len(prefix) :]
    return None, title


def _render_page(
    out: Path,
    findings: Findings,
    sub_files: list[str] | None = None,
    base_url: str | None = None,
    flow_cards: list[dict] | None = None,
) -> Path:
    """Writes report.html, summary.md and report-files.txt (the page and every image it shows, for
    a publish step) into out. Images outside out, or not PNG files, are left out."""
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    root = out.resolve()
    shown: list[str] = []

    def t(s) -> str:
        s = html.escape(str(s), quote=False)
        s = re.sub(r"\*\*([^*\n]+)\*\*", r"<b>\1</b>", s)
        s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
        return re.sub(r"\b((?:device|app|wc|rabby)/report\.html)\b", r'<a href="\1">\1</a>', s)

    def img(rel: str | None, alt: str, photo: bool = False, extra_cls: str = "") -> str:
        if not rel:
            return '<div class="absent">none</div>'
        p = (root / rel).resolve()
        if not p.is_relative_to(root) or p.suffix != ".png" or not p.is_file():
            return '<div class="absent">image missing</div>'
        rel_str = str(p.relative_to(root))
        r = html.escape(rel_str)
        shown.append(rel_str)
        classes = " ".join(c for c in ("photo" if photo else "", extra_cls) if c)
        cls = f' class="{classes}"' if classes else ""
        return f'<a href="{r}" target="_blank"><img src="{r}" alt="{html.escape(alt)}"{cls} loading="lazy"></a>'

    def screen(s: dict | None, side_cls: str = "") -> str:
        if s is None:
            return '<div class="absent">no screen</div>'
        fields = dict(s["fields"])
        title = fields.pop("TITLE", None) or fields.pop("title", None)
        body = [f'<div class="kind">{t(s["kind"])}</div>']
        if title:
            body.append(f'<div class="t">{t(title)}</div>')
        body += [f'<div><span class="k">{t(k.lower())}:</span> {t(v)}</div>' for k, v in fields.items()]
        cls = f"screen {side_cls}".strip()
        return f'<div class="{cls}">' + "".join(body) + "</div>"

    def pairs(screens: list[list[dict | None]]) -> str:
        h = ['<div class="pairs"><div class="h">Device screen before</div><div class="h">After</div>']
        for a, b in screens:
            h += [screen(a, "before"), screen(b, "after")]
        return "".join(h) + "</div>"

    def compare(rows: list[CompareRow], photo: bool = False) -> str:
        if not rows:
            return ""
        marked = any(r.marked for r in rows)
        heads = '<div class="h">Before</div><div class="h">After</div>' + (
            '<div class="h">Changed (red)</div>' if marked else ""
        )
        h = [f'<div class="cmp{"" if marked else " two"}">']
        for i, r in enumerate(rows):
            if r.label:
                h.append(f'<div class="label">{t(r.label)}</div>')
            if i == 0:
                h.append(heads)
            ph = photo or r.photo
            h += [img(r.before, "before", ph), img(r.after, "after", ph, "after-ok" if not ph else "")]
            if marked:
                h.append(img(r.marked, "marked", ph, "diff-hit") if r.marked else "<div></div>")
        return "".join(h) + "</div>"

    def filmstrip(rows: list[CompareRow], photo: bool = False) -> str:
        items = []
        for r in rows:
            rel = r.after or r.before
            if not rel:
                continue
            ph = photo or r.photo
            cap = f"<figcaption>{t(r.label)}</figcaption>" if r.label else ""
            items.append(f"<figure>{img(rel, r.label or 'screen', ph)}{cap}</figure>")
        return '<div class="filmstrip">' + "".join(items) + "</div>" if items else ""

    def excerpt(text: str) -> str:
        lines = text.splitlines()
        pre = f"<pre>{html.escape(text)}</pre>"
        return (
            f"<details><summary>Advanced output &amp; trace ({plural(len(lines), 'line')})</summary>"
            f"<div>{pre}</div></details>"
        )

    def card_title(raw_title: str, pill_cls: str) -> str:
        flow_name, rest = _split_flow_prefix(raw_title)
        if flow_name:
            return (
                f'<div class="card-title-row"><span class="pill {pill_cls}">{t(flow_name)}</span>'
                f"<h3>{t(rest)}</h3></div>"
            )
        return f"<h3>{t(raw_title)}</h3>"

    def ul(items) -> str:
        return "<ul>" + "".join(f"<li>{t(i)}</li>" for i in items) + "</ul>"

    f = replace(findings, changes=merge_similar(findings.changes))
    top, sub = headline(f)
    banner = "fail" if f.broke else "warn" if (f.incomplete or f.changes) else "pass"
    h = [
        "<main><header>",
        f'<div class="eyebrow">BitBox e2e · {t(f.repo or "report")}</div>',
        f"<h1>{t(f.title)}</h1>",
    ]
    if f.sides:
        h.append(
            '<div class="sides-bar">'
            f'<span class="side-chip"><b>Before</b> {t(f.sides["base"])}</span>'
            '<span class="arrow">→</span>'
            f'<span class="side-chip"><b>After</b> {t(f.sides["head"])}</span>'
            "</div>"
        )
    h.append("</header>")

    extra = f" · {plural(len(f.known), 'pre-existing issue')}" if f.known else ""
    h.append(
        f'<section class="hero {banner}"><div class="hero-top">'
        f'<div class="big">{t(top)}</div>'
        f'<div class="sub">{t(sub[0].upper() + sub[1:])}{t(extra)}</div></div>'
    )
    if flow_cards:
        h.append('<div class="scorecard">')
        for fc in flow_cards:
            st = fc["status"]
            pill_cls = {"fail": "bad", "warn": "warn", "changed": "acc", "pass": "good"}.get(st, "muted")
            href = fc.get("href")
            link = f'<a href="{html.escape(href)}">Open flow report →</a>' if href else "See workflow log"
            h.append(
                f'<div class="flow-tile {st}">'
                f'<div class="flow-tile-head"><span class="flow-tile-name">{t(fc["name"])}</span>'
                f'<span class="pill {pill_cls}">{t(fc["badge"])}</span></div>'
                f'<div class="flow-tile-sub">{t(fc["detail"])}</div>'
                f'<div class="flow-tile-foot">{link}</div></div>'
            )
        h.append("</div>")
    h.append("</section>")

    h += [f'<div class="callout">{t(c)}</div>' for c in f.cautions]

    if f.broke:
        h.append(
            f'<h2>What broke <span class="pill bad">{plural(len(f.broke), "regression")}</span></h2>'
            '<p class="sec-sub">Passed before this PR and fails with it. Expand trace blocks for raw test output.</p>'
        )
        for b in f.broke:
            h.append(f'<article class="card bad"><div class="card-head">{card_title(b.title, "bad")}</div>')
            if b.detail:
                h.append(f'<p class="card-detail">{t(b.detail)}</p>')
            h.append(compare(b.compare, b.photo))
            if b.screens:
                h.append(
                    f"<details><summary>Device screen text diff ({len(b.screens)})</summary>"
                    f"<div>{pairs(b.screens)}</div></details>"
                )
            if b.excerpt:
                h.append(excerpt(b.excerpt))
            h.append("</article>")

    if f.changes:
        h.append(
            f'<h2>What changed on the device <span class="pill acc">{plural(len(f.changes), "change")}</span></h2>'
            '<p class="sec-sub">Device screens and wallet views that changed while tests still passed.</p>'
        )
        for c in f.changes:
            h.append(f'<article class="card chg"><div class="card-head">{card_title(c.title, "acc")}</div>')
            if c.where:
                more = f" and {len(c.where) - 1} more" if len(c.where) > 1 else ""
                h.append(f'<p class="where">Where: {t(f"`{c.where[0]}`")}{t(more)}</p>')
            h.append(compare(c.compare, c.photo))
            if c.screens:
                h.append(pairs(c.screens))
            h += [f'<p class="muted">{t(n)}</p>' for n in c.notes]
            if len(c.where) > 1:
                items_html = ul(f"`{w}`" for w in c.where)
                h.append(
                    f"<details><summary>All affected scenarios ({len(c.where)})</summary>"
                    f"<div>{items_html}</div></details>"
                )
            if c.variants:
                h.append(
                    f"<details><summary>Other value variants of this change ({len(c.variants)})"
                    "</summary><div>"
                )
                for v in c.variants:
                    h.append(f'<p class="where">In {", ".join(t(f"`{w}`") for w in v.where)}:</p>')
                    if v.screens:
                        h.append(pairs(v.screens))
                    h.append(compare(v.compare, v.photo))
                h.append("</div></details>")
            h.append("</article>")
    else:
        h.append(
            '<h2>What changed on the device <span class="pill good">No changes</span></h2>'
            '<p class="sec-sub">No device screen is different with this PR.</p>'
        )

    if f.fixed:
        h += [
            f'<h2>Fixed by this PR <span class="pill good">{len(f.fixed)}</span></h2>'
            '<p class="sec-sub">Failed before this PR and passes with it.</p>',
            f'<div class="card">{ul(f.fixed)}</div>',
        ]

    if f.proof:
        open_attr = " open" if (not f.broke and not f.changes) else ""
        h.append(
            f"<details{open_attr}><summary>Proof: screens and screenshots from passing flows ({len(f.proof)})"
            "</summary><div>"
        )
        for p_item in f.proof:
            body = filmstrip(p_item.compare, p_item.photo)
            head = card_title(p_item.title, "good")
            h.append(f'<article class="card"><div class="card-head">{head}</div>{body}</article>')
        h.append("</div></details>")

    if f.followups:
        h += ["<h2>To do after merge</h2>", f'<div class="card">{ul(f.followups)}</div>']

    # Reference & advanced details — collapsed so the primary deck stays crisp and scannable.
    h.append('<section class="ref-section"><h2>Reference &amp; advanced details</h2>')
    if f.tested:
        h.append(f"<details><summary>What was tested ({len(f.tested)})</summary><div>{ul(f.tested)}</div></details>")
    if f.minor:
        h.append(
            f"<details><summary>Pixel-only differences, same screen text ({len(f.minor)} — usually capture timing)"
            "</summary><div>"
        )
        for m in f.minor:
            h.append(f'<article class="card"><h3>{t(m.title)}</h3>{compare(m.compare, m.photo)}</article>')
        h.append("</div></details>")
    if f.known:
        h.append(
            f"<details><summary>Already failing before this PR ({len(f.known)} — not caused by this PR)</summary>"
            f"<div>{ul(f.known)}</div></details>"
        )
    if f.details or f.notes:
        rows_html = "".join(f"<tr><th>{t(k)}</th><td>{t(b)}</td><td>{t(a)}</td></tr>" for k, b, a in f.details)
        h.append(
            "<details><summary>Versions, test matrix &amp; log locations</summary><div>"
            + (f"<table><tr><th></th><th>Before</th><th>After</th></tr>{rows_html}</table>" if rows_html else "")
            + (ul(f.notes) if f.notes else "")
            + "</div></details>"
        )
    h.append("</section></main>")

    page_title = html.escape(f"e2e: {f.title}")
    page = (
        f'<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" '
        f'content="width=device-width,initial-scale=1"><title>{page_title}</title><style>{CSS}</style>\n'
        + "\n".join(h)
        + "\n</html>\n"
    )
    (out / "report.html").write_text(page)
    (out / "summary.md").write_text(summary(f, base_url=base_url, root=root, flow_cards=flow_cards))
    files = list(dict.fromkeys(["report.html", *shown, *(sub_files or [])]))
    (out / "report-files.txt").write_text("\n".join(files) + "\n")
    return out / "report.html"


def render_flow(flow_dir: Path, findings: Findings) -> Path:
    """Write findings.json, report.html, summary.md, and report-files.txt inside flow_dir."""
    flow_dir = Path(flow_dir)
    flow_dir.mkdir(parents=True, exist_ok=True)
    (flow_dir / "findings.json").write_text(json.dumps(findings.to_dict(), indent=1))
    return _render_page(flow_dir, findings)


def render_run(out_dir: Path, base_url: str | None = None) -> Path:
    """Aggregate out_dir/<flow> for flow in FLOW_ORDER into root report.html, summary.md, and
    report-files.txt. Idempotent: reads only sub-flow files and writes only root files."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    agg = Findings(title="")
    loaded_flows: list[str] = []
    flow_cards: list[dict] = []

    for flow in FLOW_ORDER:
        d = out_dir / flow
        err_path = d / "flow-error.json"
        if err_path.exists():
            e = json.loads(err_path.read_text())
            name = FLOW_NAMES[e.get("flow", flow)]
            first = next((line for line in reversed(e["error"].splitlines()) if line.strip()), "no error message")
            agg.cautions.append(f"{name} could not run, so it tested nothing: {first[:300]}")
            agg.incomplete.append(name)
            flow_cards.append(
                {
                    "flow": flow,
                    "name": name,
                    "status": "warn",
                    "badge": "Could not run",
                    "detail": first[:140],
                    "href": None,
                }
            )
            continue
        p = d / "findings.json"
        if not p.exists():
            continue
        loaded_flows.append(flow)
        sub = Findings.from_dict(json.loads(p.read_text()))
        name = FLOW_NAMES[flow]
        if not agg.title and sub.title:
            agg.title = sub.title
        if not agg.repo and sub.repo:
            agg.repo = sub.repo
        if not agg.sides and sub.sides:
            agg.sides = dict(sub.sides)
        agg.broke.extend(
            FindingItem(
                title=f"{name}: {b.title}",
                detail=b.detail,
                excerpt=b.excerpt,
                screens=list(b.screens),
                compare=[r.rebase(flow) for r in b.compare],
                photo=b.photo,
            )
            for b in sub.broke
        )
        agg.fixed.extend(f"{name}: {x}" for x in sub.fixed)
        agg.known.extend(f"{name}: {x}" for x in sub.known)
        agg.followups.extend(f"{name}: {x}" for x in sub.followups)
        agg.cautions.extend(f"{name}: {x}" for x in sub.cautions)
        agg.changes.extend(_rebase_change(c, flow, name) for c in sub.changes)
        agg.minor.extend(_rebase_change(m, flow, name) for m in sub.minor)
        agg.proof.extend(_rebase_change(p_item, flow, name) for p_item in sub.proof)
        sub_merged = replace(sub, changes=merge_similar(sub.changes))
        top, rest = headline(sub_merged)
        agg.tested.append(
            f"{name}: {sub.scope or 'see its report'}. Result: {top}, {rest}. Its own report: {flow}/report.html"
        )
        if sub_merged.broke:
            st, badge = "fail", plural(len(sub_merged.broke), "regression")
            detail = sub_merged.broke[0].title
        elif sub_merged.changes:
            st, badge = "changed", plural(len(sub_merged.changes), "screen change")
            detail = sub_merged.changes[0].title
        else:
            st, badge = "pass", "Passed"
            detail = (
                plural(len(sub_merged.proof), "proof capture")
                if sub_merged.proof
                else "No regressions or screen diffs"
            )
        flow_cards.append(
            {
                "flow": flow,
                "name": name,
                "status": st,
                "badge": badge,
                "detail": detail[:140],
                "href": f"{flow}/report.html",
            }
        )

    if not agg.title:
        if agg.incomplete:
            agg.title = f"{' and '.join(agg.incomplete)} could not run"
        else:
            agg.title = "BitBox e2e report"

    sub_files = [
        f"{flow}/{line}"
        for flow in loaded_flows
        if (out_dir / flow / "report-files.txt").exists()
        for line in (out_dir / flow / "report-files.txt").read_text().splitlines()
        if line.strip()
    ]
    return _render_page(out_dir, agg, sub_files=sub_files, base_url=base_url, flow_cards=flow_cards)


def summary(
    f: Findings,
    base_url: str | None = None,
    root: Path | None = None,
    flow_cards: list[dict] | None = None,
) -> str:
    """Markdown for the CI run page and the PR comment: the result, per-flow status matrix,
    one line per finding, and (when base_url is provided) hosted HTML report link + inline PNGs."""

    def md(s: str) -> str:
        """Text from the code under test must not become links or markup in a comment; code spans
        stay as they are."""
        parts = re.split(r"(`[^`]*`)", str(s))
        return "".join(x if x.startswith("`") else re.sub(r"([\\\[\]<>!*_|#])", r"\\\1", x) for x in parts)

    base = base_url.rstrip("/") if base_url else None
    resolved_root = root.resolve() if root else None

    def safe_img_url(rel: str | None) -> str | None:
        if not base or not rel:
            return None
        if resolved_root is not None:
            p = (resolved_root / rel).resolve()
            if not p.is_relative_to(resolved_root) or p.suffix != ".png" or not p.is_file():
                return None
            rel = str(p.relative_to(resolved_root))
        elif not rel.endswith(".png") or rel.startswith("/") or ".." in Path(rel).parts:
            return None
        return f"{base}/{quote(rel, safe='/-._~')}"

    def compare_table(rows: list[CompareRow], limit: int = 2) -> list[str]:
        rendered: list[tuple[str | None, str | None, str | None]] = []
        for r in rows:
            bu, au, mu = safe_img_url(r.before), safe_img_url(r.after), safe_img_url(r.marked)
            if bu or au or mu:
                rendered.append((bu, au, mu))
                if len(rendered) >= limit:
                    break
        if not rendered:
            return []
        has_marked = any(mu for _, _, mu in rendered)
        cols = ["Before", "After", "Diff"] if has_marked else ["Before", "After"]
        out_lines = [
            "| " + " | ".join(cols) + " |",
            "| " + " | ".join(["---"] * len(cols)) + " |",
        ]
        for bu, au, mu in rendered:
            cells = [f"![before]({bu})" if bu else "—", f"![after]({au})" if au else "—"]
            if has_marked:
                cells.append(f"![diff]({mu})" if mu else "—")
            out_lines.append("| " + " | ".join(cells) + " |")
        return out_lines

    f_merged = replace(f, changes=merge_similar(f.changes))
    top, sub = headline(f_merged)
    icon = "❌" if f_merged.broke else "⚠️" if (f_merged.incomplete or f_merged.changes) else "✅"
    lines = [f"### {icon} {top} · {sub}", ""]
    if base:
        lines += [f"**[Open full HTML report]({base}/report.html)**", ""]
    if flow_cards:
        st_icon = {"fail": "❌", "warn": "⚠️", "changed": "🔎", "pass": "✅"}
        lines += [
            "| Flow | Status | Summary |",
            "| --- | --- | --- |",
        ]
        for fc in flow_cards:
            ic = st_icon.get(fc["status"], "•")
            flow_lbl = (
                f"[{md(fc['name'])}]({base}/{fc['href']})"
                if base and fc.get("href")
                else md(fc["name"])
            )
            lines.append(f"| {flow_lbl} | {ic} {md(fc['badge'])} | {md(fc['detail'])} |")
        lines.append("")
    if f_merged.incomplete:
        lines += [f"- {md(c)}" for c in f_merged.cautions if "could not run" in c] + [""]
    if f_merged.broke:
        lines += ["**What broke**", ""]
        shown_broke = 0
        for b in f_merged.broke:
            lines.append(f"- {md(b.title)}")
            if base and shown_broke < 3 and (tbl := compare_table(b.compare, limit=2)):
                lines += ["", *tbl, ""]
                shown_broke += 1
        lines.append("")
    if f_merged.fixed:
        lines += ["**Fixed by this PR**", ""] + [f"- {md(x)}" for x in f_merged.fixed] + [""]
    if f_merged.changes:
        lines += ["**What changed on the device**", ""]
        shown_chg = 0
        for c in f_merged.changes:
            at = (
                f" in `{c.where[0]}`" + (f" and {len(c.where) - 1} more" if len(c.where) > 1 else "")
                if c.where
                else ""
            )
            lines.append(f"- {md(c.title)}{md(at)}")
            if base and shown_chg < 3 and (tbl := compare_table(c.compare, limit=2)):
                lines += ["", *tbl, ""]
                shown_chg += 1
        lines.append("")
    if base and f_merged.proof:
        proof_imgs: list[tuple[str, str]] = []
        for p_item in f_merged.proof:
            for r in p_item.compare:
                if url := safe_img_url(r.after or r.before):
                    proof_imgs.append((r.label or p_item.title, url))
                    break
            if len(proof_imgs) >= 4:
                break
        if proof_imgs:
            lines += [
                "<details><summary>Proof screenshots from passing flows</summary>",
                "",
                "| Flow / Step | Screenshot |",
                "| --- | --- |",
                *(f"| {md(lbl)} | ![proof]({url}) |" for lbl, url in proof_imgs),
                "",
                "</details>",
                "",
            ]
    if f_merged.known:
        n_known = plural(len(f_merged.known), "problem")
        lines += [f"{n_known} already there before this PR (not caused by it): see the report.", ""]
    return "\n".join(lines)
