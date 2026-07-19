"""Cross the live-shaped resident -> receipt-emitter seam from issue #46.

These tests start at CMUX JSONL frames, not hand-built ``SourceEventRef``
objects. Reopen the defect if the honest marker no longer validates or if a
wire signature from another authority is accepted as the authoritative one.

The literal Codex duplicate sequence is tracked separately by issue #59. This
fixture represents one logical submit so this file proves the producer seam
without silently choosing #59's normalization policy.
"""

from __future__ import annotations

import pytest
from yatagarasu_cmux import EventOutbox, EventStreamResident, UnixCmuxSocketClient
from yatagarasu_cmux.receipt_producer import DerivedEventReceiptProducer

from yatagarasu_core import (
    CorrelationRule,
    Delivery,
    DeliveryMode,
    EvidenceClass,
    ProofMethodRegistration,
    SourceKind,
)
from yatagarasu_core.proofs import MarkerAuthority, validate_session_proof

from .socket_harness import CmuxSocketHarness, ack, event, short_socket_path

SOURCE_INSTANCE = "cmux-producer-vesper"
PROVIDER_ID = "cmux-provider"
PROOF_METHOD = "cmux.event_bus.harness_hook_relay"
ISSUED_AT = "2026-07-19T20:00:00Z"
EXPIRES_AT = "2026-07-19T20:05:00Z"
OBSERVED_AT = "2026-07-19T20:01:00Z"
SESSION_ID = "codex-live-session"
REAL_KEY = b"authoritative-producer-key"
ATTACKER_KEY = b"untrusted-wire-marker-key"


@pytest.fixture
def delivery() -> Delivery:
    return Delivery(
        "event-live",
        "delivery-live",
        "attempt-live",
        "binding-live",
        "yua",
        DeliveryMode.SESSION_BOUND,
    )


def _registration() -> ProofMethodRegistration:
    return ProofMethodRegistration(
        proof_method=PROOF_METHOD,
        source_kind=SourceKind.EVENT_BUS,
        source_instance_id=SOURCE_INSTANCE,
        correlation_rule=CorrelationRule.CMUX_HARNESS_CHAIN,
        evidence_classes=frozenset({EvidenceClass.HARNESS_TURN_COMPLETED}),
    )


def _run_live_shaped_chain(tmp_path, delivery, wire_token, authoritative_marker):
    emitted = []
    producer = DerivedEventReceiptProducer(
        core_client=emitted.append,
        provider_id=PROVIDER_ID,
        delivery_lookup=lambda delivery_id: (
            (delivery, authoritative_marker)
            if delivery_id == delivery.delivery_id
            else None
        ),
    )
    frames = [
        ack(
            "boot-live",
            replay_count=0,
            gap=False,
            requested_after_seq=None,
            latest_seq=4,
        ),
        event("boot-live", 1, name="surface.input_sent"),
        event(
            "boot-live",
            2,
            name="workspace.prompt.submitted",
            payload={"message_preview": f"{wire_token} payload omitted"},
        ),
        event(
            "boot-live",
            3,
            name="agent.hook.UserPromptSubmit",
            payload={"session_id": SESSION_ID, "hook_event_name": "UserPromptSubmit"},
        ),
        event(
            "boot-live",
            4,
            name="agent.hook.Stop",
            payload={"session_id": SESSION_ID, "hook_event_name": "Stop"},
        ),
    ]
    socket_path = short_socket_path(tmp_path, "receipt-producer")
    with EventOutbox(tmp_path / "events.sqlite") as outbox:
        with CmuxSocketHarness(socket_path, [frames]):
            run = EventStreamResident(
                source_instance_id=SOURCE_INSTANCE,
                client=UnixCmuxSocketClient(socket_path),
                outbox=outbox,
                marker_key=b"legacy-projector-key",
                receipt_producer=producer,
            ).run()

    assert run.inserted_event_count == 4
    assert len(emitted) == 1
    return emitted[0]


def test_live_shaped_frames_cross_resident_and_validate(tmp_path, delivery) -> None:
    authority = MarkerAuthority(REAL_KEY)
    authoritative = authority.mint(
        delivery, issued_at=ISSUED_AT, expires_at=EXPIRES_AT
    )

    receipt = _run_live_shaped_chain(
        tmp_path, delivery, authority.encode(authoritative), authoritative
    )

    assert receipt.proof is not None
    assert (
        validate_session_proof(
            proof=receipt.proof,
            delivery=delivery,
            evidence_class=receipt.evidence_class,
            registration=_registration(),
            marker_authority=authority,
            observed_at=OBSERVED_AT,
        )
        is None
    )


def test_wire_signature_mismatch_survives_translation_and_is_rejected(
    tmp_path, delivery
) -> None:
    authority = MarkerAuthority(REAL_KEY)
    attacker = MarkerAuthority(ATTACKER_KEY)
    authoritative = authority.mint(
        delivery, issued_at=ISSUED_AT, expires_at=EXPIRES_AT
    )
    forged = attacker.mint(delivery, issued_at=ISSUED_AT, expires_at=EXPIRES_AT)
    assert forged.signature != authoritative.signature

    receipt = _run_live_shaped_chain(
        tmp_path, delivery, attacker.encode(forged), authoritative
    )

    assert receipt.proof is not None
    prompt = receipt.proof.source_events[1]
    assert prompt.marker_signature == forged.signature
    assert (
        validate_session_proof(
            proof=receipt.proof,
            delivery=delivery,
            evidence_class=receipt.evidence_class,
            registration=_registration(),
            marker_authority=authority,
            observed_at=OBSERVED_AT,
        )
        == "prompt_marker_binding_mismatch"
    )
