"""What lets the harness run outside this machine: no fixed ports or remote names."""

import socket
import subprocess

import pytest

from bitbox_e2e import config, runs, units


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


def commit_all(repo, msg):
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "add", ".")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", msg)
    return git(repo, "rev-parse", "HEAD")


# --- upstream remote ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "git@github.com:BitBoxSwiss/bitbox02-firmware.git",
        "https://github.com/BitBoxSwiss/bitbox02-firmware",
        "https://github.com/bitboxswiss/bitbox02-firmware.git/",
        "ssh://git@github.com/BitBoxSwiss/bitbox02-firmware.git",
    ],
)
def test_upstream_is_the_remote_pointing_at_the_slug(tmp_path, url):
    git(tmp_path, "init", "-q")
    git(tmp_path, "remote", "add", "origin", "git@github.com:someone/bitbox02-firmware.git")
    git(tmp_path, "remote", "add", "up", url)
    assert config.upstream_remote(tmp_path, "BitBoxSwiss/bitbox02-firmware") == "up"


def test_upstream_falls_back_to_origin(tmp_path):
    git(tmp_path, "init", "-q")
    git(tmp_path, "remote", "add", "fork", "git@github.com:someone/bitbox02-firmware.git")
    # A fork whose name merely ends in the slug is not upstream.
    git(tmp_path, "remote", "add", "x", "git@github.com:NotBitBoxSwiss/bitbox02-firmware-old.git")
    assert config.upstream_remote(tmp_path, "BitBoxSwiss/bitbox02-firmware") == "origin"


# --- ports ----------------------------------------------------------------------------------------


def sim_bind(port):
    """How both simulators bind: 0.0.0.0, no SO_REUSEADDR."""
    s = socket.socket()
    s.bind(("0.0.0.0", port))
    return s


def test_free_port_is_bindable_by_a_simulator():
    sim_bind(config.free_port()).close()


def test_listening_port_is_not_free():
    s = sim_bind(config.free_port())
    s.listen()
    try:
        with pytest.raises(RuntimeError):
            config.wait_port_free(s.getsockname()[1], timeout_s=0.3)
    finally:
        s.close()


# --- graphical simulator --port -------------------------------------------------------------------


def fake_sim(tmp_path, help_text):
    d = tmp_path / "sim"
    d.mkdir(parents=True)
    exe = d / "simulator-graphical"
    exe.write_text(f"#!/bin/sh\ncat <<'EOF'\n{help_text}\nEOF\n")
    exe.chmod(0o755)
    return d


def test_graphical_port_option_is_detected_per_binary(tmp_path):
    new = fake_sim(tmp_path / "a", "Options:\n      --preseed\n      --port <PORT>  TCP port [default: 15423]")
    old = fake_sim(tmp_path / "b", "Options:\n      --preseed\n  -h, --help")
    assert config.takes_port(new) is True
    assert config.takes_port(old) is False


def test_missing_graphical_binary_has_no_port_option(tmp_path):
    assert config.takes_port(tmp_path) is False


# --- firmware refs without the BTC vectors file ---------------------------------------------------


def test_ref_without_vectors_loads_the_other_units(tmp_path, monkeypatch):
    repo = tmp_path / "fw"
    repo.mkdir()
    git(repo, "init", "-q")
    (repo / "README").write_text("old firmware\n")
    old = commit_all(repo, "before the vectors file")
    vectors = repo / config.FW_VECTORS
    vectors.parent.mkdir(parents=True)
    vectors.write_text('{"vectors": []}\n')
    new = commit_all(repo, "vectors")
    monkeypatch.setattr(config, "FW", repo)
    monkeypatch.setitem(config.REPOS, "firmware", config.REPOS["firmware"]._replace(dir=repo))
    monkeypatch.setattr(config, "CACHE", tmp_path / "cache")

    old_dir = config.firmware_dir(old)
    assert not config.has_vectors(old_dir)
    ids = [u["id"] for u in units.load_units(old_dir)]
    assert ids and not any(i.startswith("btc-vector/") for i in ids)
    assert config.has_vectors(config.firmware_dir(new))


# --- revisions under test -------------------------------------------------------------------------


def test_revision_differences_ignore_unknown_sides():
    base = {"driver bitbox-api-rs": None, "api-rs": "a", "api-go": "g"}
    head = {"driver bitbox-api-rs": "d+dirty", "api-rs": "b", "api-go": "g", "api-ts": "t"}
    assert runs.revision_diffs(base, head) == ["api-rs"]
