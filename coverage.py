#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["pillow>=10", "python-xlib>=0.33"]
# ///
"""Which e2e units execute which firmware Rust code, and which units a diff needs.

  coverage.py map <ref> [-j N]                 instrumented build, every unit headless, map.json
  coverage.py select --base <sha> --head <ref|WORKTREE> [--map <ref>] [--summary]

map.json ($BITBOX_E2E_CACHE/baselines/<sha>/coverage/map.json):
  functions  {"<file>::<function>": [unit ids]}   generics stripped, closures are their own entry
  ranges     {"<file>::<function>": [first line, last line]}
  lines      {"<file>": {"<line>": [unit ids]}}    every instrumented line, [] if no unit runs it
Only Rust under src/rust/ linked into the simulator, minus test code. C is not instrumented.
"""

import argparse
import collections
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

E2E_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(E2E_DIR))
import e2e  # noqa: E402
import units  # noqa: E402

CACHE = Path(os.environ.get("BITBOX_E2E_CACHE", Path.home() / ".cache" / "bitbox-e2e"))
FW = Path(os.environ.get("BITBOX_FIRMWARE", E2E_DIR.parent / "bitbox02-firmware"))
SKIP = re.compile(r"/tests?/|/build/[^/]+/out/|/testing/|/benches/|/bins?/|^src/rust/(bitbox-test-vectors|vendor|async_test)/")


def log(msg):
    print(f"[coverage] {msg}", file=sys.stderr, flush=True)


def git(*args):
    return subprocess.run(["git", "-C", str(FW), *args], check=True, capture_output=True, text=True).stdout


def rev(ref):
    return git("rev-parse", "--verify", f"{ref}^{{commit}}").strip()


def map_path(sha):
    return CACHE / "baselines" / sha / "coverage" / "map.json"


def load_units(sha):
    return units.load_units(FW, E2E_DIR / "scenarios", ref=sha)


# --- map ------------------------------------------------------------------------------------------

def norm_path(p):
    """Firmware-relative path of a coverage source file, or None if it isn't firmware Rust we track."""
    # rustc remaps <fw>/src/rust to "src" and llvm-cov prefixes the compilation dir "src" again.
    # Builds under $HOME get "$HOME=" remapped instead, leaving .../bitbox02-firmware-<sha>/src/rust/...
    if p.startswith("src/src/"):
        p = "src/rust/" + p[8:]
    elif "bitbox02-firmware" in p and "/src/rust/" in p:
        p = p[p.rindex("/src/rust/") + 1:]
    else:
        return None
    if "rustlib" in p or ".cargo" in p or SKIP.search(p):
        return None
    return p


def strip_generics(name):
    # Drop generic argument lists (`foo::<T>`, `Foo<T>`) but keep qualified paths (`<A as B>::f`).
    out, depth = [], 0
    for i, c in enumerate(name):
        if c == "<" and (depth or (i and (name[i - 1].isalnum() or name[i - 1] in "_:"))):
            depth += 1
        elif c == ">" and depth:
            depth -= 1
        elif not depth:
            out.append(c)
    return "".join(out).replace("::::", "::").replace(":: ", " ").rstrip(":")


def ident(fn):
    """`<X as T>::sign::{closure#0}` -> `sign`: the source-level fn name a parser sees."""
    segs, depth, cur = [], 0, ""
    for c in fn:
        depth += (c == "<") - (c == ">")
        cur += c
        if not depth and cur.endswith("::"):
            segs.append(cur[:-2])
            cur = ""
    segs.append(cur)
    segs = [s for s in segs if s and not s.startswith("{")]
    return segs[-1] if segs else fn


def llvm_tool(name, toolchain):
    sysroot = subprocess.run(["rustc", f"+{toolchain}", "--print", "sysroot"], check=True,
                             capture_output=True, text=True).stdout.strip()
    return f"{sysroot}/lib/rustlib/x86_64-unknown-linux-gnu/bin/{name}"


def toolchain(sha):
    for f in ("rust-toolchain.toml", "src/rust/rust-toolchain.toml"):
        try:
            return re.search(r'^channel = "(.*)"', git("show", f"{sha}:{f}"), re.M).group(1)
        except subprocess.CalledProcessError:
            pass
    raise RuntimeError(f"no rust-toolchain.toml at {sha}")


def function_table(cov, profdata, binary):
    """mangled name -> (key, file, first line, last line) for every tracked function in the binary."""
    out = subprocess.run([cov, "export", "-format=text", "-skip-expansions", "-instr-profile", profdata, binary],
                         check=True, capture_output=True, text=True).stdout
    funcs = {}
    for f in json.loads(out)["data"][0]["functions"]:
        path = norm_path(f["filenames"][0])
        regions = [r for r in f["regions"] if r[5] == 0 and r[7] in (0, 3)]
        if path and regions:
            funcs[f["name"]] = (path, min(r[0] for r in regions), max(r[2] for r in regions))
    names = list(funcs)
    # binutils' c++filt (gcc is a prerequisite anyway) does Rust v0; drop its crate disambiguators.
    demangled = subprocess.run(["c++filt"], input="\n".join(names), check=True,
                               capture_output=True, text=True).stdout.splitlines()
    demangled = [strip_generics(re.sub(r"\[[0-9a-f]+\]", "", d)) for d in demangled]
    return {m: (f"{funcs[m][0]}::{d}", *funcs[m]) for m, d in zip(names, demangled)}


def run_units(sim_dir, work, sha, shard, shards):
    """Runs inside isolated.sh: this shard's units headless, one profile directory each."""
    sim_dir, work = Path(sim_dir), Path(work)
    for unit in load_units(sha)[shard::shards]:
        d = work / units.slug(unit["id"])
        (d / "profraw").mkdir(parents=True, exist_ok=True)
        (d / "scenario.json").write_text(json.dumps(unit["scenario"]))
        # %c: continuous mode, counters are mmapped to the file and survive the simulator being killed.
        os.environ["LLVM_PROFILE_FILE"] = str(d / "profraw" / "%c%p.profraw")
        try:
            ok = e2e.run_headless(sim_dir, d / "scenario.json", d / "headless")
        except Exception as exc:  # noqa: BLE001 - one broken unit must not sink the map
            log(f"{unit['id']}: {exc}")
            ok = False
        (d / "ok").write_text("1" if ok else "0")


def cmd_map(args):
    sha = rev(args.ref)
    sim_dir = Path(subprocess.run([str(E2E_DIR / "build-sim.sh"), "--coverage", sha], check=True,
                                  stdout=subprocess.PIPE, text=True).stdout.strip().splitlines()[-1])
    e2e.ensure_driver()
    unit_ids = [u["id"] for u in load_units(sha)]
    work = Path(args.work or f"/tmp/e2e-cov/{sha[:12]}")
    shutil.rmtree(work, ignore_errors=True)
    log(f"{len(unit_ids)} units, {args.jobs} shards, work dir {work}")
    # Each shard gets its own network namespace, so the simulators' fixed port doesn't collide.
    shards = [subprocess.Popen([str(E2E_DIR / "isolated.sh"), sys.executable, __file__, "_run", str(sim_dir),
                                str(work), sha, str(i), str(args.jobs)]) for i in range(args.jobs)]
    if any(p.wait() for p in shards):
        raise RuntimeError("a unit shard crashed")

    tc = toolchain(sha)
    profdata_tool, cov = llvm_tool("llvm-profdata", tc), llvm_tool("llvm-cov", tc)
    binary = str(sim_dir / "simulator")
    lines = collections.defaultdict(lambda: collections.defaultdict(set))
    hits = collections.defaultdict(set)
    failed, table = [], None
    for uid in unit_ids:
        d = work / units.slug(uid)
        if not (d / "ok").exists() or (d / "ok").read_text() != "1":
            failed.append(uid)
        raws = glob.glob(str(d / "profraw" / "*.profraw"))
        if not raws:
            continue
        pd = str(d / "unit.profdata")
        subprocess.run([profdata_tool, "merge", "-sparse", "-o", pd, *raws], check=True)
        if table is None:
            # Function records don't depend on the profile; any unit's will do for names and ranges.
            table = function_table(cov, pd, binary)
        lcov = subprocess.run([cov, "export", "-format=lcov", r"-ignore-filename-regex=rustlib|/vendor/|\.cargo",
                               "-instr-profile", pd, binary], check=True, capture_output=True, text=True).stdout
        path = None
        for line in lcov.splitlines():
            if line.startswith("SF:"):
                path = norm_path(line[3:])
            elif path and line.startswith("DA:"):
                n, count = line[3:].split(",")[:2]
                (lines[path][int(n)].add(uid) if int(count) else lines[path][int(n)])
            elif path and line.startswith("FNDA:"):
                count, name = line[5:].split(",", 1)
                if int(count) and name in table:
                    hits[table[name][0]].add(uid)
    if table is None:
        raise RuntimeError(f"no profiles under {work}; did the instrumented simulator run?")

    ranges = {}
    for key, _, first, last in table.values():
        a, b = ranges.get(key, (first, last))
        ranges[key] = [min(a, first), max(b, last)]
    out = map_path(sha)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "sha": sha,
        "units": unit_ids,
        "failed": failed,
        "functions": {k: sorted(hits[k]) for k in sorted(ranges)},
        "ranges": dict(sorted(ranges.items())),
        "lines": {f: {str(n): sorted(u) for n, u in sorted(ls.items())} for f, ls in sorted(lines.items())},
    }))
    reached = sum(1 for k in ranges if hits[k])
    log(f"{len(ranges)} functions ({reached} reached), {len(lines)} files, {len(failed)} failed units")
    print(out)


# --- select ---------------------------------------------------------------------------------------

def diff_hunks(base, head):
    """[(base path, head path, base lines, head lines, head anchor)] for Rust files under src/rust/.
    Paths are None for added/deleted files; the anchor is where a pure deletion sits in head."""
    cmd = ["diff", "-U0", "-M", base] + ([] if head == "WORKTREE" else [head]) + ["--", "src/rust/*.rs"]
    hunks, a, b = [], None, None
    for line in git(*cmd).splitlines():
        if line.startswith("--- "):
            a = line[6:] if line.startswith("--- a/") else None
        elif line.startswith("+++ "):
            b = line[6:] if line.startswith("+++ b/") else None
        elif line.startswith("@@"):
            m = re.match(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", line)
            s1, n1, s2, n2 = int(m[1]), int(m[2] or 1), int(m[3]), int(m[4] or 1)
            hunks.append((a, b, list(range(s1, s1 + n1)), list(range(s2, s2 + n2)), s2))
    return hunks


_FN_RULE = """id: fn
language: rust
rule:
  kind: function_item
  not:
    inside:
      kind: mod_item
      has: {field: name, regex: "^tests?$"}
      stopBy: end
"""
_fn_cache = {}


def source_functions(path, ref):
    """[(first line, last line, name)] of non-test fn items in `path` at `ref` (WORKTREE: on disk)."""
    if (path, ref) not in _fn_cache:
        src = (FW / path).read_text() if ref == "WORKTREE" else git("show", f"{ref}:{path}")
        with tempfile.NamedTemporaryFile("w", suffix=".rs") as f:
            f.write(src)
            f.flush()
            out = subprocess.run(["sg", "scan", "--inline-rules", _FN_RULE, "--json=compact", f.name],
                                 check=True, capture_output=True, text=True).stdout
        _fn_cache[(path, ref)] = [(m["range"]["start"]["line"] + 1, m["range"]["end"]["line"] + 1,
                                   re.search(r"\bfn\s+(\w+)", m["text"])[1]) for m in json.loads(out)]
    return _fn_cache[(path, ref)]


def innermost(spans, line):
    inside = [s for s in spans if s[0] <= line <= s[1]]
    return min(inside, key=lambda s: s[1] - s[0]) if inside else None


def select(base_sha, head_ref, map_ref=None):
    base = rev(base_sha)
    m = json.loads(map_path(rev(map_ref) if map_ref else base).read_text())
    # A map from another commit (approximation) has different line numbers, so go by fn name only.
    exact = m["sha"] == base
    by_file = collections.defaultdict(list)
    for key, (first, last) in m["ranges"].items():
        f, fn = key.split("::", 1)
        by_file[f].append((first, last, key, ident(fn)))

    changed = {}  # (base file, fn name) -> {"file", "function", "lines", "units"}

    def touch(path, name, line, hit):
        keys = [k for _, _, k, i in by_file.get(path, []) if i == name]
        fn = min((k.split("::", 1)[1] for k in keys), key=len, default=name)
        c = changed.setdefault((path, name), {"file": path, "function": fn, "lines": set(), "units": set()})
        c["lines"].add(line)
        c["units"] |= set(hit)

    def name_units(path, name):
        return set().union(*(m["functions"][k] for _, _, k, i in by_file.get(path, []) if i == name))

    for a, b, alines, blines, anchor in diff_hunks(base, head_ref):
        if a is None:
            # New file: count it if its crate is linked at all.
            crate = "/".join(b.split("/")[:3]) + "/"
            if SKIP.search(b) or not any(f.startswith(crate) for f in m["lines"]):
                continue
        elif a not in m["lines"]:
            continue  # not linked into the simulator, or test code
        hline = blines[0] if blines else anchor
        for n in alines:
            if exact:
                span = innermost(by_file[a], n)
                if span:
                    # Executed lines select precisely; others (comments, continuation lines) by function.
                    hit = m["lines"][a].get(str(n))
                    touch(a, span[3], hline, m["functions"][span[2]] if hit is None else hit)
            elif fn := innermost(source_functions(a, base), n):
                touch(a, fn[2], hline, name_units(a, fn[2]))
        for n in blines if b else []:
            fn = innermost(source_functions(b, head_ref), n)
            path = a or b
            known = any(i == fn[2] for *_, i in by_file.get(path, [])) if fn else False
            if not fn or (exact and alines and known):
                continue  # the base side of this hunk already selected precisely
            # Pure insertions and new functions have no base line: look the head fn up by name in base.
            touch(path, fn[2], n, name_units(path, fn[2]))

    selected = set().union(*(c["units"] for c in changed.values()))
    unreached = [{"file": c["file"], "function": c["function"], "lines": sorted(c["lines"])}
                 for c in changed.values() if not c["units"]]
    return {"base": base, "map": m["sha"], "units": sorted(selected), "unreached": unreached,
            "changed_functions": len(changed)}


def summary(r):
    s = [f"{r['changed_functions']} changed functions, {len(r['units'])} units selected"
         + ("" if r["map"] == r["base"] else f" (map from {r['map'][:9]}, matched by fn name)")]
    s += [f"  {u}" for u in r["units"]]
    s.append(f"{len(r['unreached'])} changed functions no unit executes:")
    s += [f"  {u['file']}:{u['lines'][0]}  {u['function']}" for u in r["unreached"]]
    return "\n".join(s)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("map")
    p.add_argument("ref")
    p.add_argument("-j", "--jobs", type=int, default=4)
    p.add_argument("--work", help="profiles and per-unit logs (default /tmp/e2e-cov/<sha>)")
    p = sub.add_parser("select")
    p.add_argument("--base", required=True)
    p.add_argument("--head", required=True)
    p.add_argument("--map", help="use the map of another commit (approximate, by fn name)")
    p.add_argument("--summary", action="store_true")
    p = sub.add_parser("_run")
    p.add_argument("args", nargs=5)
    args = ap.parse_args()
    if args.cmd == "map":
        cmd_map(args)
    elif args.cmd == "select":
        r = select(args.base, args.head, args.map)
        print(summary(r) if args.summary else json.dumps(r, indent=2))
    else:
        sim_dir, work, sha, i, n = args.args
        run_units(sim_dir, work, sha, int(i), int(n))


if __name__ == "__main__":
    main()
