"""coverage.select on a throwaway firmware repo: what the map cannot see is reported, not dropped."""

import json
import subprocess

import pytest

import coverage
import e2e

LIB = "src/rust/app/src/lib.rs"


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def fw(tmp_path, monkeypatch):
    repo = tmp_path / "fw"
    (repo / "src/rust/app/src").mkdir(parents=True)
    (repo / "messages").mkdir()
    (repo / LIB).write_text("fn f() {\n    let a = 1;\n}\n")
    (repo / "src/ui.c").write_text("int x;\n")
    (repo / "messages/eth.proto").write_text("message A {}\n")
    git(repo, "init", "-q")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "add", ".")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "base")
    base = git(repo, "rev-parse", "HEAD")
    cache = tmp_path / "cache"
    m = cache / "baselines" / base / "coverage" / "map.json"
    m.parent.mkdir(parents=True)
    m.write_text(json.dumps({"sha": base, "units": ["u1"], "failed": [],
                             "functions": {f"{LIB}::app::f": ["u1"]}, "ranges": {f"{LIB}::app::f": [1, 3]},
                             "lines": {LIB: {"1": ["u1"], "2": ["u1"], "3": ["u1"]}}}))
    monkeypatch.setattr(coverage, "FW", repo)
    monkeypatch.setattr(coverage, "CACHE", cache)
    coverage._fn_cache.clear()
    return repo, base


def commit(repo, path, text):
    (repo / path).write_text(text)
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam", "head")
    return git(repo, "rev-parse", "HEAD")


def test_change_outside_mapped_rust_is_listed_as_unmapped(fw):
    repo, base = fw
    commit(repo, "messages/eth.proto", "message A { int32 b = 1; }\n")
    head = commit(repo, "src/ui.c", "int y;\n")
    r = coverage.select(base, head)
    assert r["units"] == [] and r["unreached"] == []
    assert r["unmapped"] == ["messages/eth.proto", "src/ui.c"]
    note = e2e.unreached_note(r["unreached"], r["unmapped"])
    assert "none" not in note and "src/ui.c" in note


def test_change_in_mapped_rust_selects_its_units(fw):
    repo, base = fw
    head = commit(repo, LIB, "fn f() {\n    let a = 2;\n}\n")
    r = coverage.select(base, head)
    assert r["units"] == ["u1"] and r["unmapped"] == []


def test_missing_ast_grep_is_a_clear_error(fw, monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    with pytest.raises(RuntimeError, match="ast-grep"):
        coverage.source_functions(LIB, "WORKTREE")
