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
from base64 import urlsafe_b64decode, urlsafe_b64encode
from binascii import Error as Base64Error
from dataclasses import dataclass
from hashlib import sha256
from typing import Final

from yatagarasu_core.proofs import DeliveryMarker, MarkerAuthority
from yatagarasu_core.proofs import MarkerError as CoreMarkerError

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
_SHORT_PREFIX: Final = "ygr1s"
_TOKEN_ID_PATTERN: Final = r"[A-Za-z0-9_-]{1,64}"
_SHORT_SIGNATURE_CHARS: Final = 43
_SHORT_MARKER_RE: Final = re.compile(
    rf"(?P<token>{_SHORT_PREFIX}\."
    rf"(?P<delivery_id>{_TOKEN_ID_PATTERN})\."
    rf"(?P<binding_id>{_TOKEN_ID_PATTERN})\."
    rf"(?P<signature>[A-Za-z0-9_-]{{{_SHORT_SIGNATURE_CHARS}}}))"
    rf"(?![A-Za-z0-9_-])"
)
MAX_SHORT_MARKER_CHARS: Final = 180


@dataclass(frozen=True, slots=True)
class ShortMarker:
    """The independently observed correlation fields that fit a CMUX preview."""

    delivery_id: str
    binding_id: str
    signature: str

    @property
    def text(self) -> str:
        signature_bytes = bytes.fromhex(self.signature)
        signature = urlsafe_b64encode(signature_bytes).decode().rstrip("=")
        return f"{_SHORT_PREFIX}.{self.delivery_id}.{self.binding_id}.{signature}"


def encode_short(marker: DeliveryMarker) -> str:
    """Encode only wire-observed correlation fields under the 240-char cap."""
    id_pattern = re.compile(rf"^{_TOKEN_ID_PATTERN}$")
    if not id_pattern.fullmatch(marker.delivery_id):
        raise MarkerError("delivery_id cannot be represented in a short marker")
    if not id_pattern.fullmatch(marker.binding_id):
        raise MarkerError("binding_id cannot be represented in a short marker")
    if not re.fullmatch(r"[0-9a-f]{64}", marker.signature):
        raise MarkerError("marker signature is not a full SHA-256 hex digest")
    token = ShortMarker(marker.delivery_id, marker.binding_id, marker.signature).text
    if len(token) > MAX_SHORT_MARKER_CHARS:
        raise MarkerError("short marker exceeds the CMUX preview budget")
    return token


def extract(haystack: str | None) -> DeliveryMarker | ShortMarker | None:
    """Extract and parse a marker from arbitrary text.

    Returns ``None`` when no decodable marker is present. Note that decoding is
    NOT authorization. The caller must still validate the marker against the
    delivery lookup to ensure the signature and binding are correct.

    The caller is expected to keep only the returned marker and drop ``haystack``.

    Note: this function used to take a ``key`` parameter that was never read in
    the body. The signature was a vestige of an earlier authorization path that
    moved to core; the marker verification now lives in
    ``core.MarkerAuthority.validate``. Removal was driven by issue #57's
    stored-and-never-read sweep.
    """
    if not haystack:
        return None

    short = _SHORT_MARKER_RE.search(haystack)
    if short is not None:
        encoded_signature = short.group("signature")
        try:
            signature_bytes = urlsafe_b64decode(encoded_signature + "=")
        except (ValueError, Base64Error):
            signature_bytes = b""
        canonical = urlsafe_b64encode(signature_bytes).decode().rstrip("=")
        if len(signature_bytes) == 32 and canonical == encoded_signature:
            return ShortMarker(
                delivery_id=short.group("delivery_id"),
                binding_id=short.group("binding_id"),
                signature=signature_bytes.hex(),
            )

    # Signature validation lives in MarkerAuthority.validate, which needs the
    # delivery record; this function only parses the wire format.
    #
    # Returns the FIRST decodable marker, not all of them. The comment here used
    # to say "yield ALL decodable markers it finds" above a `return` in the loop
    # body — a description of a generator, over code that is not one.
    for match in _YGR1_RE.finditer(haystack):
        try:
            return MarkerAuthority.decode(match.group(1))
        except CoreMarkerError:
            # CoreMarkerError, not this module's MarkerError. They are unrelated
            # classes — `issubclass(core.MarkerError, marker.MarkerError)` is
            # False — so catching the local one here would catch nothing and let
            # decode errors escape. Narrowing to the wrong exception is a guard
            # that looks tighter and is simply absent.
            #
            # Narrow on purpose either way: `except Exception` swallows our own
            # bugs, so a TypeError in decode would read as "no marker present"
            # and the delivery would look like it was never marked.
            continue

    return None


def marker_text(marker: DeliveryMarker | ShortMarker) -> str:
    """Return the exact safe marker token without surrounding prompt content."""
    if isinstance(marker, ShortMarker):
        return marker.text
    return MarkerAuthority.encode(marker)


def redact(haystack: str | None) -> str:
    """Describe text without reproducing it.

    Used anywhere a preview would otherwise be logged. Returns only a length, so
    an operator can tell "something was there" without the content leaving the
    host-local boundary.
    """
    return f"<redacted len={len(haystack)}>" if haystack else "<empty>"
