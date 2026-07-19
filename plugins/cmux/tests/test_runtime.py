"""Tests for runtime discovery.

The resident starts without a cmux parent shell, so every one of these is about
failing loudly at startup rather than starting into a broken state. A transport
that comes up without a usable socket reports healthy and delivers nothing.
"""

from __future__ import annotations

import os
import socket
import stat
import tempfile
from pathlib import Path

import pytest
from yatagarasu_cmux.runtime import (
    RuntimeDiscoveryError,
    describe,
    discover_socket_path,
    load,
    verify_socket,
)


@pytest.fixture
def bound_socket():
    """Bind unix sockets that are always closed.

    The project sets ``filterwarnings = ["error"]``, so a leaked socket becomes a
    test failure rather than a quiet ResourceWarning. That config caught the
    first draft of this file, which closed socket objects in ``finally`` but
    still handed out unmanaged handles from a helper.
    """
    opened: list[socket.socket] = []
    # AF_UNIX paths are capped around 104 bytes on macOS, and pytest's tmp_path
    # is comfortably longer than that. Binding under a short directory keeps
    # these tests about permissions rather than about path length.
    tmp = tempfile.TemporaryDirectory(prefix="ygr", dir="/tmp")
    short_root = Path(tmp.name)

    def _bind(name, mode=0o600):
        path = short_root / Path(name).name
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.bind(str(path))
        os.chmod(path, mode)
        opened.append(sock)
        return path

    yield _bind

    for sock in opened:
        sock.close()
    # Clean up the short-path directory too: the first draft fixed the socket
    # leak and introduced a directory leak in its place.
    tmp.cleanup()


def test_explicit_path_wins_over_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("CMUX_SOCKET_PATH", str(tmp_path / "from-env.sock"))
    assert discover_socket_path(tmp_path / "explicit.sock").name == "explicit.sock"


def test_environment_wins_over_xdg_default(tmp_path, monkeypatch):
    monkeypatch.setenv("CMUX_SOCKET_PATH", str(tmp_path / "from-env.sock"))
    assert discover_socket_path().name == "from-env.sock"


def test_falls_back_to_xdg_state_not_config(tmp_path, monkeypatch):
    """Verified against a live install: cmux uses the XDG STATE directory. The
    config-directory path some docs imply does not exist."""
    monkeypatch.delenv("CMUX_SOCKET_PATH", raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    assert discover_socket_path() == tmp_path / "cmux" / "cmux.sock"


def test_missing_socket_fails_loudly_with_a_useful_hint(tmp_path):
    with pytest.raises(RuntimeDiscoveryError, match="not found"):
        verify_socket(tmp_path / "absent.sock")


def test_a_regular_file_is_not_accepted_as_a_socket(tmp_path):
    impostor = tmp_path / "not-a-socket"
    impostor.write_text("")
    with pytest.raises(RuntimeDiscoveryError, match="not a socket"):
        verify_socket(impostor)


def test_group_or_world_accessible_socket_is_refused(tmp_path, bound_socket):
    """Filesystem permissions ARE the authentication boundary for same-user
    access. If the socket is open wider than its owner, that boundary is not
    closed and the operator must know before we start injecting."""
    path = bound_socket(tmp_path / "loose.sock", mode=0o660)
    with pytest.raises(RuntimeDiscoveryError, match="beyond its owner"):
        verify_socket(path)


def test_owner_only_socket_is_accepted(tmp_path, bound_socket):
    path = bound_socket(tmp_path / "tight.sock", mode=0o600)
    assert verify_socket(path) == path
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_load_resolves_a_complete_config(tmp_path, monkeypatch, bound_socket):
    sock = bound_socket(tmp_path / "cmux.sock")
    monkeypatch.setenv("YATAGARASU_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("CMUX_SOCKET_PASSWORD", raising=False)

    cfg = load(socket_path=sock, source_instance_id="host-a")

    assert cfg.socket_path == sock
    assert cfg.source_instance_id == "host-a"
    assert cfg.password is None
    assert cfg.journal_path.parent == cfg.state_dir
    assert cfg.outbox_path.parent == cfg.state_dir
    assert cfg.state_dir.exists()


def test_source_instance_defaults_to_hostname(tmp_path, monkeypatch, bound_socket):
    """Half of the cursor identity. Must be stable across restarts of the same
    resident and distinct between hosts."""
    sock = bound_socket(tmp_path / "cmux.sock")
    monkeypatch.setenv("YATAGARASU_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("YATAGARASU_SOURCE_INSTANCE_ID", raising=False)

    assert load(socket_path=sock).source_instance_id == os.uname().nodename


def test_password_is_optional_and_never_printed(tmp_path, monkeypatch, bound_socket):
    sock = bound_socket(tmp_path / "cmux.sock")
    monkeypatch.setenv("YATAGARASU_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("CMUX_SOCKET_PASSWORD", "hunter2")

    cfg = load(socket_path=sock, source_instance_id="host-a")

    assert cfg.password == "hunter2"
    assert "hunter2" not in describe(cfg)
    assert "<set>" in describe(cfg)


def test_describe_says_which_auth_is_in_use(tmp_path, monkeypatch, bound_socket):
    sock = bound_socket(tmp_path / "cmux.sock")
    monkeypatch.setenv("YATAGARASU_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("CMUX_SOCKET_PASSWORD", raising=False)

    assert "0600" in describe(load(socket_path=sock, source_instance_id="h"))


# --- regression tests for the review findings on this module ---


def test_tilde_in_xdg_state_home_is_expanded(monkeypatch):
    """A literal tilde would resolve to a directory named "~" in the process
    working directory, silently breaking discovery."""
    monkeypatch.setenv("XDG_STATE_HOME", "~/.local/state")
    monkeypatch.delenv("CMUX_SOCKET_PATH", raising=False)

    resolved = discover_socket_path()
    assert "~" not in str(resolved)
    assert resolved == Path.home() / ".local" / "state" / "cmux" / "cmux.sock"


def test_socket_owned_by_another_user_is_refused(tmp_path, bound_socket, monkeypatch):
    """A 0600 socket owned by someone else passes both the type and the
    permission-breadth checks and is still unusable. The mode is only an
    authentication boundary when we are the owner it is closed around."""
    path = bound_socket(tmp_path / "other.sock", mode=0o600)
    monkeypatch.setattr(os, "getuid", lambda: os.stat(path).st_uid + 1)

    with pytest.raises(RuntimeDiscoveryError, match="owned by uid"):
        verify_socket(path)


def test_fixture_cleans_up_its_temp_directory(tmp_path, bound_socket):
    """The first draft fixed a socket leak and introduced a directory leak."""
    path = bound_socket(tmp_path / "cleanup.sock")
    assert path.parent.exists()
    assert path.parent.name.startswith("ygr")
