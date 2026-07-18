"""Canonical delivery and receipt types for the Round-1 core."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class DeliveryMode(StrEnum):
    SESSION_BOUND = "session-bound"
    CHANNEL_NATIVE = "channel-native"


class DeliveryState(StrEnum):
    QUEUED = "queued"
    DISPATCHING = "dispatching"
    TRANSPORT_SUBMITTED = "transport-submitted"
    IN_SESSION = "in-session"
    PROCESSED = "processed"


class Disposition(StrEnum):
    COMPLETED = "completed"
    ANSWERED = "answered"
    ACKNOWLEDGED = "acknowledged"
    HELD = "held"
    DECLINED = "declined"


class EvidenceClass(StrEnum):
    TRANSPORT_SUBMIT_ACK = "transport.submit_ack"
    HARNESS_PROMPT_ACCEPTED = "harness.prompt_accepted"
    HARNESS_TURN_STARTED = "harness.turn_started"
    HARNESS_TURN_COMPLETED = "harness.turn_completed"
    SESSION_REPLY_AUTHORED = "session.reply_authored"
    SESSION_REACTION_AUTHORED = "session.reaction_authored"
    SESSION_DISPOSITION_AUTHORED = "session.disposition_authored"
    PARTICIPANT_REPLY_AUTHORED = "participant.reply_authored"
    PARTICIPANT_REACTION_AUTHORED = "participant.reaction_authored"


class ProviderKind(StrEnum):
    SESSION_TRANSPORT = "session-transport"
    COMMS_VIEW = "comms-view"


@dataclass(frozen=True, slots=True)
class Delivery:
    event_id: str
    delivery_id: str
    attempt_id: str
    binding_id: str
    recipient_id: str
    delivery_mode: DeliveryMode
    state: DeliveryState = DeliveryState.QUEUED
    disposition: Disposition | None = None


@dataclass(frozen=True, slots=True)
class Receipt:
    receipt_id: str
    event_id: str
    delivery_id: str
    attempt_id: str
    binding_id: str
    evidence_provider_id: str
    evidence_class: EvidenceClass
    proof_method: str
    observed_at: str
    source_event_id: str | None = None
    platform_principal_id: str | None = None
    platform_message_id: str | None = None
    disposition: Disposition | None = None
    authored_by_provider: bool = False
    infrastructure_event: bool = False


@dataclass(frozen=True, slots=True)
class ReceiptResult:
    status: str
    reason: str | None = None
    state: DeliveryState | None = None
    disposition: Disposition | None = None
