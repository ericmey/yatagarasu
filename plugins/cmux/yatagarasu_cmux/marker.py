"""Signed delivery markers.

A marker is the only thing that ties an observed host event back to a specific
delivery attempt. It exists because a session identifier alone is **not**
sufficient proof: a session identifier match shows that *a* turn happened in that
session, not that *our* message caused it. A concurrent injection, or a human
typing into the same composer, satisfies a session-only check.

The marker travels inside the injected text so it appears in the host's submit
event. Everything downstream extracts the marker and **discards the surrounding
text** — the event carries a preview of real user content, so the raw preview must
never reach receipt storage, audit records, journals, cursors, retry queues, or
error logs.
"""

from __future__ import annotations

import hmac
import re
import secrets
from dataclasses import dataclass
from hashlib import sha256
from typing import Final

_PREFIX: Final = "ygr"
_NONCE_BYTES: Final = 8
_SIG_CHARS: Final = 16

# Deliberately strict: the marker is machine-generated, so anything that does not
# match exactly is not a marker we minted.
_MARKER_RE: Final = re.compile(
    rf"\[{_PREFIX}:(?P<delivery_id>[A-Za-z0-9_-]{{1,64}})"
    rf":(?P<nonce>[0-9a-f]{{{_NONCE_BYTES * 2}}})"
    rf":(?P<sig>[0-9a-f]{{{_SIG_CHARS}}})\]"
)


class MarkerError(ValueError):
    """Raised when a marker cannot be minted or is not authentic."""


@dataclass(frozen=True, slots=True)
class Marker:
    """A minted delivery marker."""

    delivery_id: str
    nonce: str
    signature: str

    @property
    def text(self) -> str:
        """The literal token embedded in the injected message."""
        return f"[{_PREFIX}:{self.delivery_id}:{self.nonce}:{self.signature}]"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.text


def _sign(key: bytes, delivery_id: str, nonce: str) -> str:
    payload = f"{delivery_id}:{nonce}".encode()
    return hmac.new(key, payload, sha256).hexdigest()[:_SIG_CHARS]


def mint(key: bytes, delivery_id: str) -> Marker:
    """Mint a marker for one delivery attempt.

    The nonce makes each attempt distinguishable, so a retry is never mistaken
    for the original and vice versa.
    """
    if not key:
        raise MarkerError("signing key must not be empty")
    if not delivery_id or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", delivery_id):
        raise MarkerError(f"delivery_id is not a valid token: {delivery_id!r}")

    nonce = secrets.token_hex(_NONCE_BYTES)
    return Marker(delivery_id, nonce, _sign(key, delivery_id, nonce))


def extract(key: bytes, haystack: str | None) -> Marker | None:
    """Extract and verify a marker from arbitrary text.

    Returns ``None`` when no authentic marker is present. A forged or corrupted
    marker is treated exactly like no marker at all — this function never raises
    on untrusted input, because it is fed host event payloads.

    The caller is expected to keep only the returned marker and drop ``haystack``.
    """
    # An empty key makes every signature computable by anyone, so a misconfigured
    # deployment would accept forged markers rather than failing closed. Reject
    # before comparing, and do it here rather than trusting callers: this function
    # exists precisely to be safe on untrusted input.
    if not key or not haystack:
        return None

    for match in _MARKER_RE.finditer(haystack):
        delivery_id = match.group("delivery_id")
        nonce = match.group("nonce")
        signature = match.group("sig")
        if hmac.compare_digest(signature, _sign(key, delivery_id, nonce)):
            return Marker(delivery_id, nonce, signature)

    return None


def redact(haystack: str | None) -> str:
    """Describe text without reproducing it.

    Used anywhere a preview would otherwise be logged. Returns only a length, so
    an operator can tell "something was there" without the content leaving the
    host-local boundary.
    """
    return f"<redacted len={len(haystack)}>" if haystack else "<empty>"
