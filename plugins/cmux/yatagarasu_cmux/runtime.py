"""Runtime discovery for a host-resident plugin.

The resident is started by a supervisor (launchd, systemd, or a human running it
in a terminal). It therefore has **no cmux-spawned parent shell**, which is the
usual source of ``CMUX_SOCKET_PATH`` and friends. Everything it needs must be
discoverable from a bare environment.

Verified against a live install rather than assumed:

- The socket is at ``$XDG_STATE_HOME/cmux/cmux.sock``, defaulting to
  ``~/.local/state/cmux/cmux.sock``. It is **not** at ``~/.config/cmux/cmux.sock``,
  which some documentation implies and which does not exist on a real install.
- The socket mode is ``0600``. **Filesystem permissions are the authentication
  boundary** for a same-user process; no password is required. A password only
  matters for cross-user or remote access, so the resident treats one as optional.
- A completely clean environment (``env -i``) can connect and issue commands, so
  no inherited cmux state is needed.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path

#: Where cmux actually puts its control socket.
_SOCKET_RELATIVE = Path("cmux") / "cmux.sock"

#: Documented-but-wrong location, checked only to produce a useful error.
_LEGACY_HINT = Path("cmux") / "cmux.sock"


class RuntimeDiscoveryError(RuntimeError):
    """Raised when the resident cannot establish how to reach cmux.

    Deliberately fails loudly at startup rather than degrading. A transport that
    silently starts without a usable socket would report healthy and deliver
    nothing, which is the failure mode this whole plugin exists to prevent.
    """


def state_home() -> Path:
    """XDG state directory, with the platform default.

    Expanded, because a user may reasonably export ``XDG_STATE_HOME=~/.local/state``
    and a literal tilde would silently resolve to a directory named "~" in the
    process working directory.
    """
    configured = os.environ.get("XDG_STATE_HOME")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".local" / "state"


def discover_socket_path(explicit: str | os.PathLike[str] | None = None) -> Path:
    """Resolve the cmux control socket.

    Precedence: explicit argument, then ``CMUX_SOCKET_PATH`` (set inside
    cmux-spawned shells), then the XDG state location.
    """
    for candidate in (explicit, os.environ.get("CMUX_SOCKET_PATH")):
        if candidate:
            return Path(candidate).expanduser()
    return state_home() / _SOCKET_RELATIVE


def verify_socket(path: Path) -> Path:
    """Confirm the path is a socket **this** process may use.

    Three checks, and the third exists because the first draft of this function
    made a claim broader than what it verified: it said "a socket this process
    may use" while only checking file type and permission breadth. A ``0600``
    socket owned by a *different* user passes both of those and is still
    unusable — the mode is only an authentication boundary if we are the owner
    it is closed around.

    This does not attempt a connection. Connecting has side effects on the
    server and would make a read-only preflight check stateful; ownership plus
    mode is what can be honestly asserted without touching cmux.
    """
    if not path.exists():
        legacy = Path.home() / ".config" / _LEGACY_HINT
        hint = (
            f" (note: {legacy} does not exist either; cmux uses the XDG state"
            " directory, not the config directory)"
            if not legacy.exists()
            else ""
        )
        raise RuntimeDiscoveryError(f"cmux socket not found at {path}{hint}")

    mode = path.stat().st_mode
    if not stat.S_ISSOCK(mode):
        raise RuntimeDiscoveryError(f"{path} exists but is not a socket")
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise RuntimeDiscoveryError(
            f"{path} is accessible beyond its owner (mode {stat.S_IMODE(mode):04o});"
            " refusing to rely on filesystem permissions for authentication"
        )

    owner = path.stat().st_uid
    if owner != os.getuid():
        raise RuntimeDiscoveryError(
            f"{path} is owned by uid {owner}, not this process (uid {os.getuid()});"
            " a 0600 socket is only a usable boundary when we are its owner"
        )
    return path


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    """Everything the resident needs to start, resolved from the environment."""

    socket_path: Path
    state_dir: Path
    password: str | None
    source_instance_id: str

    @property
    def journal_path(self) -> Path:
        return self.state_dir / "cmux-injection-journal.sqlite"

    @property
    def outbox_path(self) -> Path:
        return self.state_dir / "cmux-event-outbox.sqlite"


def load(
    *,
    socket_path: str | os.PathLike[str] | None = None,
    state_dir: str | os.PathLike[str] | None = None,
    source_instance_id: str | None = None,
) -> RuntimeConfig:
    """Build a validated runtime configuration or fail loudly.

    ``source_instance_id`` identifies *this resident on this host*. It is half of
    the cursor identity — ``(source_instance_id, boot_id, seq)`` — so it must be
    stable across restarts of the same resident and distinct between hosts. A
    hostname is the honest default; two residents on one host must be given
    distinct ids explicitly.
    """
    resolved_socket = verify_socket(discover_socket_path(socket_path))

    resolved_state = Path(
        state_dir
        or os.environ.get("YATAGARASU_STATE_DIR")
        or state_home() / "yatagarasu"
    ).expanduser()
    resolved_state.mkdir(parents=True, exist_ok=True)

    instance = (
        source_instance_id
        or os.environ.get("YATAGARASU_SOURCE_INSTANCE_ID")
        or os.uname().nodename
    )
    if not instance:
        raise RuntimeDiscoveryError(
            "source_instance_id could not be determined; set"
            " YATAGARASU_SOURCE_INSTANCE_ID"
        )

    return RuntimeConfig(
        socket_path=resolved_socket,
        state_dir=resolved_state,
        # Optional by design: the 0600 socket is the boundary for same-user access.
        password=os.environ.get("CMUX_SOCKET_PASSWORD") or None,
        source_instance_id=instance,
    )


def describe(config: RuntimeConfig) -> str:
    """Operator-facing summary. Never prints the password."""
    return (
        f"socket={config.socket_path}\n"
        f"state={config.state_dir}\n"
        f"journal={config.journal_path}\n"
        f"outbox={config.outbox_path}\n"
        f"source_instance_id={config.source_instance_id}\n"
        f"password={'<set>' if config.password else '<none — using socket mode 0600>'}"
    )
