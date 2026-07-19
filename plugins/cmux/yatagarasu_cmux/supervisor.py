"""Process entry point: wire discovery to the resident and keep it alive.

This is the piece that turns a set of libraries into something that runs. It owns
three responsibilities the resident deliberately does not:

1. **The marker key.** Markers are HMAC-signed, and the key must survive
   restarts. If it rotated on start, every marker minted before the restart would
   become unverifiable — deliveries already in flight would fail correlation and
   look like they never landed. So the key is generated once and persisted 0600.
2. **Supervision.** cmux manages the lifecycle of its own registered agents; it
   does not manage ours, because we are an automation client rather than a
   registered integration. Restart policy is therefore ours to own.
3. **Refusing to start when it cannot work.** A transport that comes up without a
   usable socket reports healthy and delivers nothing, which is the exact failure
   this project exists to prevent.
"""

from __future__ import annotations

import logging
import os
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path

from .event_outbox import EventOutbox
from .resident import EventStreamResident
from .runtime import RuntimeConfig, RuntimeDiscoveryError
from .socket_client import UnixCmuxSocketClient

log = logging.getLogger(__name__)

_MARKER_KEY_BYTES = 32


def marker_key_path(config: RuntimeConfig) -> Path:
    return config.state_dir / "marker-key"


def load_or_create_marker_key(config: RuntimeConfig) -> bytes:
    """Return the persistent marker-signing key, creating it once.

    Stability matters more than it looks. A marker proves that *this* delivery
    caused *this* turn; the key is what makes it unforgeable. Rotating it on
    restart would invalidate every marker already sitting in a composer or
    awaiting a Stop, turning correlated deliveries into uncorrelated ones —
    silent loss that looks like the agent simply never answered.
    """
    path = marker_key_path(config)

    if path.exists():
        mode = path.stat().st_mode
        if mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise RuntimeDiscoveryError(
                f"{path} is readable beyond its owner"
                f" (mode {stat.S_IMODE(mode):04o}); a leaked marker key lets"
                " anyone forge delivery proofs"
            )
        key = path.read_bytes()
        if len(key) < _MARKER_KEY_BYTES:
            raise RuntimeDiscoveryError(
                f"{path} holds {len(key)} bytes; refusing to sign with a short key"
            )
        return key

    key = secrets.token_bytes(_MARKER_KEY_BYTES)
    # Create with restrictive permissions from the start rather than chmod-ing
    # after: a world-readable window, however brief, is a leaked signing key.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, key)
    finally:
        os.close(fd)
    log.info("minted a new marker key at %s", path)
    return key


@dataclass(slots=True)
class Supervisor:
    """Builds the resident from validated config and runs it."""

    config: RuntimeConfig

    def build(self) -> tuple[EventStreamResident, EventOutbox]:
        outbox = EventOutbox(self.config.outbox_path)
        client = UnixCmuxSocketClient(
            self.config.socket_path, password=self.config.password
        )
        resident = EventStreamResident(
            source_instance_id=self.config.source_instance_id,
            client=client,
            outbox=outbox,
            marker_key=load_or_create_marker_key(self.config),
        )
        return resident, outbox

    def run_once(self, *, max_connections: int = 1):
        """One bounded supervision pass. Returns the resident's run result.

        Bounded rather than infinite by design: the caller decides the restart
        policy, and a bounded pass is what makes this testable at all. An
        unbounded loop here would be untestable and would hide reconnect storms.
        """
        resident, outbox = self.build()
        try:
            return resident.run(max_connections=max_connections)
        finally:
            outbox.close()
