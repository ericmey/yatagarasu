"""Binding-scoped delivery markers and session-proof correlation."""

from __future__ import annotations

import hashlib
import hmac
import json
from base64 import urlsafe_b64decode, urlsafe_b64encode
from binascii import Error as Base64Error
from datetime import datetime, timedelta
from itertools import pairwise

from .types import (
    CorrelationRule,
    Delivery,
    DeliveryMarker,
    EvidenceClass,
    ProofMethodRegistration,
    SessionProof,
    SourceEventRef,
    SourceKind,
)

AUTHORITY_SCOPE = "conversation"
MARKER_SCHEMA_VERSION = 1

_PROMPT_CHAIN = (
    "surface.input_sent",
    "workspace.prompt.submitted",
    "agent.hook.UserPromptSubmit",
)
_COMPLETED_CHAIN = (*_PROMPT_CHAIN, "agent.hook.Stop")


class MarkerError(ValueError):
    """An untrusted marker token could not be decoded."""


def parse_timestamp(value: str) -> datetime:
    """Parse an aware ISO-8601 timestamp, accepting the canonical Z suffix."""
    if not isinstance(value, str):
        raise ValueError("timestamp must be text")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include a timezone")
    return parsed


class MarkerAuthority:
    """Mint and verify short-lived markers with a core-held HMAC key."""

    def __init__(self, signing_key: bytes, *, max_lifetime_seconds: int = 300) -> None:
        if not signing_key:
            raise ValueError("marker signing key must not be empty")
        if max_lifetime_seconds <= 0:
            raise ValueError("marker lifetime must be positive")
        self._signing_key = signing_key
        self._max_lifetime = timedelta(seconds=max_lifetime_seconds)

    def mint(
        self, delivery: Delivery, *, issued_at: str, expires_at: str
    ) -> DeliveryMarker:
        if delivery.binding_id is None:
            raise ValueError("cannot mint a marker without an authoritative binding")
        lifetime = parse_timestamp(expires_at) - parse_timestamp(issued_at)
        if lifetime <= timedelta(0):
            raise ValueError("marker expiry must follow issue time")
        if lifetime > self._max_lifetime:
            raise ValueError("marker lifetime exceeds configured maximum")
        unsigned = DeliveryMarker(
            schema_version=MARKER_SCHEMA_VERSION,
            event_id=delivery.event_id,
            delivery_id=delivery.delivery_id,
            attempt_id=delivery.attempt_id,
            binding_id=delivery.binding_id,
            authority_scope=AUTHORITY_SCOPE,
            issued_at=issued_at,
            expires_at=expires_at,
            signature="",
        )
        return DeliveryMarker(
            schema_version=unsigned.schema_version,
            event_id=unsigned.event_id,
            delivery_id=unsigned.delivery_id,
            attempt_id=unsigned.attempt_id,
            binding_id=unsigned.binding_id,
            authority_scope=unsigned.authority_scope,
            issued_at=unsigned.issued_at,
            expires_at=unsigned.expires_at,
            signature=self._sign(unsigned),
        )

    def validate(
        self, marker: DeliveryMarker, delivery: Delivery, *, observed_at: str
    ) -> str | None:
        if not isinstance(marker, DeliveryMarker):
            return "marker_shape_invalid"
        expected_fields = (
            MARKER_SCHEMA_VERSION,
            delivery.event_id,
            delivery.delivery_id,
            delivery.attempt_id,
            delivery.binding_id,
            AUTHORITY_SCOPE,
        )
        actual_fields = (
            marker.schema_version,
            marker.event_id,
            marker.delivery_id,
            marker.attempt_id,
            marker.binding_id,
            marker.authority_scope,
        )
        if actual_fields != expected_fields:
            return "marker_fields_mismatch"
        if not isinstance(marker.signature, str) or not hmac.compare_digest(
            marker.signature, self._sign(marker)
        ):
            return "marker_signature_invalid"
        try:
            issued = parse_timestamp(marker.issued_at)
            expires = parse_timestamp(marker.expires_at)
            observed = parse_timestamp(observed_at)
        except ValueError:
            return "marker_timestamp_invalid"
        if expires - issued > self._max_lifetime:
            return "marker_lifetime_exceeds_limit"
        if expires <= issued or observed < issued or observed >= expires:
            return "marker_expired_or_not_yet_valid"
        return None

    @staticmethod
    def encode(marker: DeliveryMarker) -> str:
        """Render a marker as one bounded, URL-safe token for prompt embedding."""
        payload = json.dumps(
            {
                "attempt_id": marker.attempt_id,
                "authority_scope": marker.authority_scope,
                "binding_id": marker.binding_id,
                "delivery_id": marker.delivery_id,
                "event_id": marker.event_id,
                "expires_at": marker.expires_at,
                "issued_at": marker.issued_at,
                "schema_version": marker.schema_version,
                "signature": marker.signature,
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        return "ygr1." + urlsafe_b64encode(payload).rstrip(b"=").decode()

    @staticmethod
    def decode(token: str) -> DeliveryMarker:
        """Parse an untrusted marker token without treating it as authorization."""
        if not isinstance(token, str) or not token.startswith("ygr1."):
            raise MarkerError("marker prefix invalid")
        encoded = token.removeprefix("ygr1.")
        if not encoded or len(encoded) > 4096:
            raise MarkerError("marker size invalid")
        padding = "=" * (-len(encoded) % 4)
        try:
            payload = json.loads(urlsafe_b64decode(encoded + padding))
        except (
            Base64Error,
            ValueError,
            UnicodeDecodeError,
            json.JSONDecodeError,
        ) as exc:
            raise MarkerError("marker encoding invalid") from exc
        fields = {
            "attempt_id",
            "authority_scope",
            "binding_id",
            "delivery_id",
            "event_id",
            "expires_at",
            "issued_at",
            "schema_version",
            "signature",
        }
        if not isinstance(payload, dict) or set(payload) != fields:
            raise MarkerError("marker fields invalid")
        if not isinstance(payload["schema_version"], int) or any(
            not isinstance(payload[field], str) for field in fields - {"schema_version"}
        ):
            raise MarkerError("marker field types invalid")
        return DeliveryMarker(**payload)

    def _sign(self, marker: DeliveryMarker) -> str:
        payload = json.dumps(
            {
                "attempt_id": marker.attempt_id,
                "authority_scope": marker.authority_scope,
                "binding_id": marker.binding_id,
                "delivery_id": marker.delivery_id,
                "event_id": marker.event_id,
                "expires_at": marker.expires_at,
                "issued_at": marker.issued_at,
                "schema_version": marker.schema_version,
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        return hmac.new(self._signing_key, payload, hashlib.sha256).hexdigest()


def validate_session_proof(
    *,
    proof: SessionProof,
    delivery: Delivery,
    evidence_class: EvidenceClass,
    registration: ProofMethodRegistration,
    marker_authority: MarkerAuthority,
    observed_at: str,
) -> str | None:
    """Validate the assembled bundle without inventing fields on every event."""
    if not isinstance(proof, SessionProof) or not isinstance(proof.session_id, str):
        return "session_proof_shape_invalid"
    if evidence_class not in registration.evidence_classes:
        return "evidence_class_not_declared_for_proof_method"
    if registration.correlation_rule is not CorrelationRule.CMUX_HARNESS_CHAIN:
        return "proof_correlation_rule_unsupported"
    if registration.source_kind is not SourceKind.EVENT_BUS:
        return "proof_source_kind_mismatch"

    marker_error = marker_authority.validate(
        proof.marker, delivery, observed_at=observed_at
    )
    if marker_error:
        return marker_error

    if evidence_class in {
        EvidenceClass.HARNESS_PROMPT_ACCEPTED,
        EvidenceClass.HARNESS_TURN_STARTED,
    }:
        expected_names = _PROMPT_CHAIN
    elif evidence_class is EvidenceClass.HARNESS_TURN_COMPLETED:
        expected_names = _COMPLETED_CHAIN
    else:
        return "evidence_class_not_session_proof"

    events = proof.source_events
    if not isinstance(events, tuple) or any(
        not isinstance(event, SourceEventRef) for event in events
    ):
        return "source_event_chain_shape_invalid"
    if tuple(event.event_name for event in events) != expected_names:
        return "source_event_chain_wrong_shape"
    if any(
        event.source_instance_id != registration.source_instance_id for event in events
    ):
        return "source_instance_mismatch"
    if any(
        not isinstance(event.seq, int)
        or not event.boot_id
        or not event.source_event_id
        or event.seq < 0
        for event in events
    ):
        return "source_event_identity_invalid"
    if len({event.boot_id for event in events}) != 1:
        return "source_boot_changed_within_chain"
    if len({event.source_event_id for event in events}) != len(events):
        return "source_event_repeated_within_chain"
    if any(left.seq >= right.seq for left, right in pairwise(events)):
        return "source_event_chain_out_of_order"

    prompt = events[1]
    if (
        prompt.binding_id != delivery.binding_id
        or prompt.marker_signature != proof.marker.signature
    ):
        return "prompt_marker_binding_mismatch"

    hook_events = events[2:]
    if any(event.session_id != proof.session_id for event in hook_events):
        return "hook_session_mismatch"
    return None


def proof_storage_fields(proof: SessionProof | None) -> tuple[object, ...]:
    """Return content-free durable fields for receipt equality and auditability."""
    if proof is None:
        return (None, None, "[]", None)
    return (
        proof.session_id,
        proof.marker.signature,
        source_chain_json(proof.source_events),
        proof.turn_id,
    )


def source_chain_json(events: tuple[SourceEventRef, ...]) -> str:
    """Canonical content-free representation used for exact-chain comparison."""
    return json.dumps(
        [
            {
                "binding_id": event.binding_id,
                "boot_id": event.boot_id,
                "event_name": event.event_name,
                "marker_signature": event.marker_signature,
                "seq": event.seq,
                "session_id": event.session_id,
                "source_event_id": event.source_event_id,
                "source_instance_id": event.source_instance_id,
            }
            for event in events
        ],
        separators=(",", ":"),
        sort_keys=True,
    )
