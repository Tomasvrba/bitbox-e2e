"""coverage.select on a throwaway firmware repo: what the map cannot see is reported, not dropped."""

import json
import subprocess

import pytest

import coverage

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


def test_change_in_mapped_rust_selects_its_units(fw):
    repo, base = fw
    head = commit(repo, LIB, "fn f() {\n    let a = 2;\n}\n")
    r = coverage.select(base, head)
    assert r["units"] == ["u1"] and r["unmapped"] == []


def test_missing_ast_grep_is_a_clear_error(fw, monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    with pytest.raises(RuntimeError, match="ast-grep"):
        coverage.source_functions(LIB, "WORKTREE")


# Real SF paths from `llvm-cov export` of build-sim.sh --coverage builds. Cache layout: the clone is
# under $HOME (~/.cache/bitbox-e2e/src/...), so rustc's "$HOME=" remap wins and paths are HOME-relative,
# compilation dir joined in front. Worktree layout: the checkout is outside $HOME, so "<fw>/src/rust=src"
# applies and llvm-cov prefixes the compilation dir "src".
CACHE_CLONE = ".cache/bitbox-e2e/src/bitbox02-firmware-b59b48983057a737ec61d9d2e6c8dc18dd24e799-cov"
FIRMWARE_PATHS = {
    f"{CACHE_CLONE}/src/rust/{CACHE_CLONE}/src/rust/bitbox02-rust/src/hww/api/ethereum/sign.rs":
        "src/rust/bitbox02-rust/src/hww/api/ethereum/sign.rs",
    f"{CACHE_CLONE}/src/rust/{CACHE_CLONE}/src/rust/bitbox02-rust-c/src/async_usb.rs":
        "src/rust/bitbox02-rust-c/src/async_usb.rs",
    "src/src/bitbox02-rust-c/src/async_usb.rs": "src/rust/bitbox02-rust-c/src/async_usb.rs",
    "src/src/util/src/decimal.rs": "src/rust/util/src/decimal.rs",
    # The same layouts with a cache directory that isn't named like the firmware repo.
    "ci/cache/src/rust/ci/cache/src/rust/bitbox02-rust/src/lib.rs": "src/rust/bitbox02-rust/src/lib.rs",
    "/scratch/fw-abc/src/rust/bitbox02/src/hal.rs": "src/rust/bitbox02/src/hal.rs",
}
NOT_FIRMWARE = [
    f"{CACHE_CLONE}/external/vendor/addr2line/{CACHE_CLONE}/external/vendor/addr2line/src/frame.rs",
    f"{CACHE_CLONE}/external/vendor/addr2line/.rustup/toolchains/1.96.0-x86_64-unknown-linux-gnu/lib/rustlib"
    "/src/rust/library/alloc/src/borrow.rs",
    "/work/bitbox02-firmware/external/vendor/addr2line/.rustup/toolchains/"
    "1.96.0-x86_64-unknown-linux-gnu/lib/rustlib/src/rust/library/alloc/src/borrow.rs",
    ".rustup/toolchains/1.96.0-x86_64-unknown-linux-gnu/lib/rustlib/src/rust/library/core/src/option.rs",
    "src/.rustup/toolchains/1.96.0-x86_64-unknown-linux-gnu/lib/rustlib/src/rust/library/core/src/option.rs",
    f"{CACHE_CLONE}/src/rust/{CACHE_CLONE}/build-e2e/src/rust/feature-c-unit-tests/release/build/"
    "bitbox02-sys-618be779f8a9b397/out/bindings.rs",
    "src/src/bitbox02-rust/src/hww/api/ethereum/tests/sign.rs",
]


@pytest.mark.parametrize("raw,want", FIRMWARE_PATHS.items())
def test_firmware_rust_maps_to_its_repo_path_wherever_it_was_built(raw, want):
    assert coverage.norm_path(raw) == want


@pytest.mark.parametrize("raw", NOT_FIRMWARE)
def test_std_vendored_generated_and_test_code_is_dropped(raw):
    assert coverage.norm_path(raw) is None
