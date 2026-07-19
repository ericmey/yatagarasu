"""Tests for marker-key persistence and the CLI entry point.

The marker key is the security-critical piece here. A marker proves that *this*
delivery caused *this* turn; the key is what makes it unforgeable. Two properties
matter and both are tested against a deliberately broken alternative:

- **Stability.** A key that rotated on restart would invalidate every marker
  already in flight — correlated deliveries silently become uncorrelated, which
  looks exactly like the agent never answered.
- **Confidentiality.** A readable key lets anyone forge delivery proofs, so a
  loose mode is a startup failure rather than a warning.
"""

from __future__ import annotations

import os
import socket
import stat
import tempfile
from pathlib import Path

import pytest
from yatagarasu_cmux.__main__ import build_parser, cmd_doctor, main
from yatagarasu_cmux.runtime import RuntimeDiscoveryError, load
from yatagarasu_cmux.supervisor import load_or_create_marker_key, marker_key_path


@pytest.fixture
def config(tmp_path, monkeypatch):
    """A validated config pointed at a real (unserved) socket.

    AF_UNIX paths cap around 104 bytes on macOS and pytest's tmp_path is longer,
    so the socket lives under a short directory. Nothing connects to it — these
    tests are about key handling, not about cmux.
    """
    monkeypatch.delenv("CMUX_SOCKET_PASSWORD", raising=False)
    with tempfile.TemporaryDirectory(prefix="ygr", dir="/tmp") as short_root:
        path = Path(short_root) / "cmux.sock"
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.bind(str(path))
            os.chmod(path, 0o600)
            yield load(
                socket_path=path,
                state_dir=tmp_path / "state",
                source_instance_id="test-host",
            )


def test_marker_key_is_stable_across_restarts(config):
    """The property the whole receipt chain rests on. A rotating key would turn
    in-flight deliveries into uncorrelated ones — silent loss, not a loud error."""
    first = load_or_create_marker_key(config)
    second = load_or_create_marker_key(config)

    assert first == second
    assert len(first) == 32


def test_marker_key_is_created_owner_only(config):
    load_or_create_marker_key(config)
    mode = marker_key_path(config).stat().st_mode

    assert stat.S_IMODE(mode) == 0o600


def test_a_readable_marker_key_is_refused(config):
    """Not a warning. A leaked signing key lets anyone forge a delivery proof,
    which defeats the only mechanism that distinguishes a real receipt."""
    load_or_create_marker_key(config)
    path = marker_key_path(config)
    path.chmod(0o644)

    with pytest.raises(RuntimeDiscoveryError, match="readable beyond its owner"):
        load_or_create_marker_key(config)


def test_a_truncated_marker_key_is_refused(config):
    """A short key signs, so nothing visibly breaks — the signatures are just
    weaker than claimed. That is precisely why it must fail at startup."""
    path = marker_key_path(config)
    path.write_bytes(b"too-short")
    path.chmod(0o600)

    with pytest.raises(RuntimeDiscoveryError, match="refusing to sign"):
        load_or_create_marker_key(config)


# --- CLI ---


def test_every_subcommand_is_reachable():
    for command in ("doctor", "smoke", "run"):
        assert build_parser().parse_args([command]).command == command


def test_doctor_reports_configuration_without_minting_the_key(config, capsys):
    """Doctor must be safe to run at any time, so it may not mint the key: doing
    so would make a diagnostic quietly change durable state.

    Note what is *not* claimed here. Doctor is not side-effect free — every
    command goes through ``runtime.load()``, which creates the state directory.
    The first draft of the CLI docstring said doctor would "touch nothing", which
    was a claim broader than the code; Copilot caught it on PR #45. Creating an
    empty directory is benign, minting a signing key is not, and the honest line
    is between them rather than at "nothing".
    """
    args = build_parser().parse_args(
        [
            "doctor",
            "--socket",
            str(config.socket_path),
            "--state-dir",
            str(config.state_dir),
        ]
    )

    assert cmd_doctor(args) == 0
    assert not marker_key_path(config).exists()
    assert "not yet minted" in capsys.readouterr().out


def test_a_missing_socket_is_an_error_not_a_traceback(tmp_path, capsys):
    """Operators get an actionable line and exit 2. A traceback for a config
    problem trains people to ignore output."""
    code = main(["doctor", "--socket", str(tmp_path / "absent.sock")])

    assert code == 2
    assert "not found" in capsys.readouterr().err
