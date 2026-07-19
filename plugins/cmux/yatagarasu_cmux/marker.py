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

from yatagarasu_core.proofs import DeliveryMarker, MarkerAuthority

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
    """An expected marker token could not be parsed."""


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


_YGR1_RE = re.compile(r"(ygr1\.[A-Za-z0-9_-]+)")


def extract(key: bytes | None, haystack: str | None) -> DeliveryMarker | None:
    """Extract and parse a marker from arbitrary text.

    Returns ``None`` when no decodable marker is present. Note that decoding is
    NOT authorization. The caller must still validate the marker against the
    delivery lookup to ensure the signature and binding are correct.

    The caller is expected to keep only the returned marker and drop ``haystack``.
    """
    if not haystack:
        return None

    # The actual signature validation uses MarkerAuthority.validate
    # which requires the payload's Delivery record to be fetched.
    # The injector module just needs the basic decoding of the wire format.
    # Therefore, extract() will simply yield ALL decodable markers it finds.
    # If the payload is completely bogus, we return None.

    for match in _YGR1_RE.finditer(haystack):
        try:
            return MarkerAuthority.decode(match.group(1))
        except Exception:
            pass

    return None


def redact(haystack: str | None) -> str:
    """Describe text without reproducing it.

    Used anywhere a preview would otherwise be logged. Returns only a length, so
    an operator can tell "something was there" without the content leaving the
    host-local boundary.
    """
    return f"<redacted len={len(haystack)}>" if haystack else "<empty>"
