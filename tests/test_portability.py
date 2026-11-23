"""What lets the harness run outside this machine: no fixed ports, remote names or node on PATH."""

import os
import socket
import subprocess
import time

import pytest

import bundle
import e2e


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


def commit_all(repo, msg):
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "add", ".")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", msg)
    return git(repo, "rev-parse", "HEAD")


# --- upstream remote --------------------------------------------------------------------------------

@pytest.mark.parametrize("url", [
    "git@github.com:BitBoxSwiss/bitbox02-firmware.git",
    "https://github.com/BitBoxSwiss/bitbox02-firmware",
    "https://github.com/bitboxswiss/bitbox02-firmware.git/",
    "ssh://git@github.com/BitBoxSwiss/bitbox02-firmware.git",
])
def test_upstream_is_the_remote_pointing_at_the_slug(tmp_path, url):
    git(tmp_path, "init", "-q")
    git(tmp_path, "remote", "add", "origin", "git@github.com:someone/bitbox02-firmware.git")
    git(tmp_path, "remote", "add", "up", url)
    assert bundle.upstream_remote(tmp_path, "BitBoxSwiss/bitbox02-firmware") == "up"


def test_upstream_falls_back_to_origin(tmp_path):
    git(tmp_path, "init", "-q")
    git(tmp_path, "remote", "add", "fork", "git@github.com:someone/bitbox02-firmware.git")
    # A fork whose name merely ends in the slug is not upstream.
    git(tmp_path, "remote", "add", "x", "git@github.com:NotBitBoxSwiss/bitbox02-firmware-old.git")
    assert bundle.upstream_remote(tmp_path, "BitBoxSwiss/bitbox02-firmware") == "origin"


# --- ports --------------------------------------------------------------------------------------------

def sim_bind(port):
    """How both simulators bind: 0.0.0.0, no SO_REUSEADDR."""
    s = socket.socket()
    s.bind(("0.0.0.0", port))
    return s


def test_free_port_is_bindable_by_a_simulator():
    sim_bind(bundle.free_port()).close()


def test_port_in_time_wait_is_not_free():
    # The server side closing first leaves the listening port in TIME_WAIT for 60s; the simulators'
    # bind fails on it, so wait_port_free must not report it free.
    port = bundle.free_port()
    srv = sim_bind(port)
    srv.listen()
    cli = socket.create_connection(("127.0.0.1", port))
    conn, _ = srv.accept()
    conn.close()
    srv.close()
    time.sleep(0.1)
    try:
        with pytest.raises(RuntimeError, match=str(port)):
            bundle.wait_port_free(port, timeout_s=0.5)
    finally:
        cli.close()


def test_listening_port_is_not_free():
    s = sim_bind(bundle.free_port())
    s.listen()
    try:
        with pytest.raises(RuntimeError):
            bundle.wait_port_free(s.getsockname()[1], timeout_s=0.3)
    finally:
        s.close()


# --- graphical simulator --port ----------------------------------------------------------------------

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
    assert bundle.takes_port(new) is True
    assert bundle.takes_port(old) is False


def test_missing_graphical_binary_has_no_port_option(tmp_path):
    assert bundle.takes_port(tmp_path) is False


# --- firmware refs without the BTC vectors file -------------------------------------------------------

def test_ref_without_vectors_loads_the_other_units(tmp_path, monkeypatch):
    repo = tmp_path / "fw"
    repo.mkdir()
    git(repo, "init", "-q")
    (repo / "README").write_text("old firmware\n")
    old = commit_all(repo, "before the vectors file")
    vectors = repo / bundle.FW_VECTORS
    vectors.parent.mkdir(parents=True)
    vectors.write_text('{"vectors": []}\n')
    new = commit_all(repo, "vectors")
    monkeypatch.setattr(bundle, "FW", repo)
    monkeypatch.setitem(bundle.REPOS, "firmware", bundle.REPOS["firmware"]._replace(dir=repo))
    monkeypatch.setattr(bundle, "CACHE", tmp_path / "cache")

    old_dir = bundle.firmware_dir(old)
    assert not bundle.has_vectors(old_dir)
    ids = [u["id"] for u in bundle.load_units(old_dir)]
    assert ids and not any(i.startswith("btc-vector/") for i in ids)
    assert bundle.has_vectors(bundle.firmware_dir(new))


# --- node 26 -------------------------------------------------------------------------------------------

def test_flows_refuse_to_run_without_node_26(monkeypatch):
    monkeypatch.delenv("BITBOX_NODE_BIN", raising=False)
    with pytest.raises(SystemExit) as e:
        bundle.use_node()
    assert "setup.sh" in str(e.value) and "env.sh" in str(e.value)


def test_node_26_goes_first_on_path(monkeypatch, tmp_path):
    monkeypatch.setenv("BITBOX_NODE_BIN", str(tmp_path))
    monkeypatch.setenv("PATH", "/usr/bin")
    bundle.use_node()
    assert os.environ["PATH"].split(os.pathsep)[0] == str(tmp_path)


# --- revisions under test ------------------------------------------------------------------------------

def test_revision_differences_ignore_unknown_sides():
    base = {"driver bitbox-api-rs": None, "api-rs": "a", "api-go": "g"}
    head = {"driver bitbox-api-rs": "d+dirty", "api-rs": "b", "api-go": "g", "api-ts": "t"}
    assert e2e.revision_diffs(base, head) == ["api-rs"]
