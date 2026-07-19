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
from yatagarasu_cmux import (
    DerivedEventReceiptProducer,
    EventOutbox,
    EventProjector,
    EventStreamResident,
    UnixCmuxSocketClient,
)
from yatagarasu_cmux.runtime import RuntimeConfig
from yatagarasu_cmux.supervisor import Supervisor

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
    source_events = [
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
    for source_event in source_events:
        source_event["occurred_at"] = OBSERVED_AT
    frames = [
        ack(
            "boot-live",
            replay_count=0,
            gap=False,
            requested_after_seq=None,
            latest_seq=4,
        ),
        *source_events,
    ]
    socket_path = short_socket_path(tmp_path, "receipt-producer")
    config = RuntimeConfig(
        socket_path=socket_path,
        state_dir=tmp_path / "state",
        password=None,
        source_instance_id=SOURCE_INSTANCE,
    )
    config.state_dir.mkdir()
    supervisor = Supervisor.with_receipts(
        config,
        core_client=emitted.append,
        provider_id=PROVIDER_ID,
        delivery_lookup=lambda delivery_id: (
            (delivery, authoritative_marker)
            if delivery_id == delivery.delivery_id
            else None
        ),
    )
    with CmuxSocketHarness(socket_path, [frames]):
        run = supervisor.run_once()

    assert run.inserted_event_count == 4
    assert len(emitted) == 1
    return emitted[0]


def test_live_shaped_frames_cross_resident_and_validate(tmp_path, delivery) -> None:
    authority = MarkerAuthority(REAL_KEY)
    authoritative = authority.mint(delivery, issued_at=ISSUED_AT, expires_at=EXPIRES_AT)

    receipt = _run_live_shaped_chain(
        tmp_path, delivery, authority.encode(authoritative), authoritative
    )

    assert receipt.proof is not None
    assert receipt.observed_at == OBSERVED_AT
    assert (
        validate_session_proof(
            proof=receipt.proof,
            delivery=delivery,
            evidence_class=receipt.evidence_class,
            registration=_registration(),
            marker_authority=authority,
            observed_at=receipt.observed_at,
        )
        is None
    )


def test_wire_signature_mismatch_survives_translation_and_is_rejected(
    tmp_path, delivery
) -> None:
    authority = MarkerAuthority(REAL_KEY)
    attacker = MarkerAuthority(ATTACKER_KEY)
    authoritative = authority.mint(delivery, issued_at=ISSUED_AT, expires_at=EXPIRES_AT)
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
            observed_at=receipt.observed_at,
        )
        == "prompt_marker_binding_mismatch"
    )


def test_receipt_sink_failure_does_not_advance_the_stream_cursor(tmp_path) -> None:
    """Reopen if a failed receipt side effect becomes an unreplayable event."""

    class FailingProducer:
        def recover(self, events) -> None:
            assert events == ()

        def observe(self, event) -> None:
            raise RuntimeError(f"receipt sink unavailable for {event.source_event_id}")

    socket_path = short_socket_path(tmp_path, "receipt-producer-failure")
    frames = [
        ack(
            "boot-failure",
            replay_count=0,
            gap=False,
            requested_after_seq=None,
            latest_seq=1,
        ),
        event("boot-failure", 1, name="surface.input_sent"),
    ]
    with EventOutbox(tmp_path / "failed-events.sqlite") as outbox:
        with CmuxSocketHarness(socket_path, [frames]):
            resident = EventStreamResident(
                source_instance_id=SOURCE_INSTANCE,
                client=UnixCmuxSocketClient(socket_path),
                outbox=outbox,
                receipt_producer=FailingProducer(),
            )
            with pytest.raises(RuntimeError, match="receipt sink unavailable"):
                resident.run()

        assert outbox.cursor(SOURCE_INSTANCE) is None
        assert outbox.outbox_rows(SOURCE_INSTANCE) == ()


def test_restart_rebuilds_an_active_chain_before_stop_arrives(
    tmp_path, delivery
) -> None:
    """Reopen if a restart between prompt acceptance and Stop loses proof."""
    authority = MarkerAuthority(REAL_KEY)
    authoritative = authority.mint(delivery, issued_at=ISSUED_AT, expires_at=EXPIRES_AT)
    token = authority.encode(authoritative)
    socket_path = short_socket_path(tmp_path, "receipt-producer-restart")
    state_dir = tmp_path / "restart-state"
    state_dir.mkdir()
    config = RuntimeConfig(socket_path, state_dir, None, SOURCE_INSTANCE)
    emitted = []

    def supervisor():
        return Supervisor.with_receipts(
            config,
            core_client=emitted.append,
            provider_id=PROVIDER_ID,
            delivery_lookup=lambda delivery_id: (
                (delivery, authoritative)
                if delivery_id == delivery.delivery_id
                else None
            ),
        )

    accepted_frames = [
        ack(
            "boot-restart",
            replay_count=0,
            gap=False,
            requested_after_seq=None,
            latest_seq=3,
        ),
        event("boot-restart", 1, name="surface.input_sent"),
        event(
            "boot-restart",
            2,
            name="workspace.prompt.submitted",
            payload={"message_preview": token},
        ),
        event(
            "boot-restart",
            3,
            name="agent.hook.UserPromptSubmit",
            payload={"session_id": SESSION_ID},
        ),
    ]
    stop_frames = [
        ack(
            "boot-restart",
            replay_count=0,
            gap=False,
            requested_after_seq=3,
            latest_seq=4,
        ),
        event(
            "boot-restart",
            4,
            name="agent.hook.Stop",
            payload={"session_id": SESSION_ID},
        ),
    ]
    for source_event in [*accepted_frames[1:], stop_frames[1]]:
        source_event["occurred_at"] = OBSERVED_AT

    with CmuxSocketHarness(socket_path, [accepted_frames]):
        first = supervisor().run_once()
    assert first.inserted_event_count == 3
    assert emitted == []

    with CmuxSocketHarness(socket_path, [stop_frames]):
        second = supervisor().run_once()
    assert second.reconnect_after_seq == (3,)
    assert len(emitted) == 1
    assert emitted[0].proof is not None
    assert [event.event_name for event in emitted[0].proof.source_events] == [
        "surface.input_sent",
        "workspace.prompt.submitted",
        "agent.hook.UserPromptSubmit",
        "agent.hook.Stop",
    ]


def _captured_duplicate_frames(wire_token: str) -> list[dict[str, object]]:
    """Literal eight-event capture from the live Codex seat in issue #59."""
    preview = {"message_preview": f"{wire_token} payload omitted"}
    prompt_hook = {
        "session_id": SESSION_ID,
        "hook_event_name": "UserPromptSubmit",
    }
    frames = [
        event("boot-live-dup", 25573, name="surface.input_sent"),
        event(
            "boot-live-dup",
            25588,
            name="workspace.prompt.submitted",
            payload=preview,
        ),
        event(
            "boot-live-dup",
            25590,
            name="agent.hook.UserPromptSubmit",
            payload=prompt_hook,
        ),
        event(
            "boot-live-dup",
            25592,
            name="workspace.prompt.submitted",
            payload=preview,
        ),
        event(
            "boot-live-dup",
            25593,
            name="agent.hook.UserPromptSubmit",
            payload=prompt_hook,
        ),
        event(
            "boot-live-dup",
            25595,
            name="workspace.prompt.submitted",
            payload=preview,
        ),
        event(
            "boot-live-dup",
            25596,
            name="agent.hook.UserPromptSubmit",
            payload=prompt_hook,
        ),
        event(
            "boot-live-dup",
            25608,
            name="agent.hook.Stop",
            payload={"session_id": SESSION_ID, "hook_event_name": "Stop"},
        ),
    ]
    for frame in frames:
        frame["occurred_at"] = OBSERVED_AT
    return frames


def test_literal_duplicate_capture_is_normalized_before_emission(delivery) -> None:
    """SEV-1 reopen: raw duplicate callbacks reach the receipt state machine."""
    authority = MarkerAuthority(REAL_KEY)
    marker = authority.mint(delivery, issued_at=ISSUED_AT, expires_at=EXPIRES_AT)
    projector = EventProjector(
        source_instance_id=SOURCE_INSTANCE,
        marker_key=b"unused-projection-key",
    )
    observed: list[tuple[str, int]] = []

    class RecordingEmitter:
        def observe(
            self, source_event, payload=None, *, observed_at, workspace_id=None
        ):
            observed.append((source_event.event_name, source_event.seq))

    producer = DerivedEventReceiptProducer(
        core_client=lambda receipt: None,
        provider_id=PROVIDER_ID,
        delivery_lookup=lambda delivery_id: (delivery, marker),
    )
    producer._emitter = RecordingEmitter()  # type: ignore[assignment]

    for frame in _captured_duplicate_frames(authority.encode(marker)):
        producer.observe(projector.project(frame, expected_boot_id="boot-live-dup"))

    assert observed == [
        ("surface.input_sent", 25573),
        ("workspace.prompt.submitted", 25588),
        ("agent.hook.UserPromptSubmit", 25590),
        ("agent.hook.Stop", 25608),
    ]


def test_literal_duplicate_capture_emits_one_valid_receipt(tmp_path, delivery) -> None:
    """The exact captured sequence must validate without a synthetic fixture."""
    authority = MarkerAuthority(REAL_KEY)
    marker = authority.mint(delivery, issued_at=ISSUED_AT, expires_at=EXPIRES_AT)
    source_events = _captured_duplicate_frames(authority.encode(marker))
    socket_path = short_socket_path(tmp_path, "literal-receipt-capture")
    config = RuntimeConfig(
        socket_path=socket_path,
        state_dir=tmp_path / "literal-state",
        password=None,
        source_instance_id=SOURCE_INSTANCE,
    )
    config.state_dir.mkdir()
    emitted = []
    supervisor = Supervisor.with_receipts(
        config,
        core_client=emitted.append,
        provider_id=PROVIDER_ID,
        delivery_lookup=lambda delivery_id: (
            (delivery, marker) if delivery_id == delivery.delivery_id else None
        ),
    )
    frames = [
        ack(
            "boot-live-dup",
            replay_count=0,
            gap=False,
            requested_after_seq=None,
            latest_seq=25608,
        ),
        *source_events,
    ]

    with CmuxSocketHarness(socket_path, [frames]):
        run = supervisor.run_once()

    assert run.inserted_event_count == 8
    assert len(emitted) == 1
    receipt = emitted[0]
    assert receipt.proof is not None
    assert [item.seq for item in receipt.proof.source_events] == [
        25573,
        25588,
        25590,
        25608,
    ]
    assert (
        validate_session_proof(
            proof=receipt.proof,
            delivery=delivery,
            evidence_class=receipt.evidence_class,
            registration=_registration(),
            marker_authority=authority,
            observed_at=receipt.observed_at,
        )
        is None
    )


def test_literal_duplicate_capture_preserves_broken_signature(
    tmp_path, delivery
) -> None:
    """Normalization must never replace observed marker evidence from core."""
    authority = MarkerAuthority(REAL_KEY)
    attacker = MarkerAuthority(ATTACKER_KEY)
    authoritative = authority.mint(delivery, issued_at=ISSUED_AT, expires_at=EXPIRES_AT)
    forged = attacker.mint(delivery, issued_at=ISSUED_AT, expires_at=EXPIRES_AT)
    source_events = _captured_duplicate_frames(attacker.encode(forged))
    socket_path = short_socket_path(tmp_path, "literal-forged-capture")
    config = RuntimeConfig(
        socket_path=socket_path,
        state_dir=tmp_path / "literal-forged-state",
        password=None,
        source_instance_id=SOURCE_INSTANCE,
    )
    config.state_dir.mkdir()
    emitted = []
    supervisor = Supervisor.with_receipts(
        config,
        core_client=emitted.append,
        provider_id=PROVIDER_ID,
        delivery_lookup=lambda delivery_id: (
            (delivery, authoritative) if delivery_id == delivery.delivery_id else None
        ),
    )
    frames = [
        ack(
            "boot-live-dup",
            replay_count=0,
            gap=False,
            requested_after_seq=None,
            latest_seq=25608,
        ),
        *source_events,
    ]

    with CmuxSocketHarness(socket_path, [frames]):
        supervisor.run_once()

    assert len(emitted) == 1
    receipt = emitted[0]
    assert receipt.proof is not None
    assert receipt.proof.source_events[1].marker_signature == forged.signature
    assert (
        validate_session_proof(
            proof=receipt.proof,
            delivery=delivery,
            evidence_class=receipt.evidence_class,
            registration=_registration(),
            marker_authority=authority,
            observed_at=receipt.observed_at,
        )
        == "prompt_marker_binding_mismatch"
    )


def test_interleaved_workspaces_keep_independent_receipt_buffers(tmp_path) -> None:
    """SEV-1 reopen: one workspace overwrites another workspace proof chain."""
    authority = MarkerAuthority(REAL_KEY)
    delivery_a = Delivery(
        "event-a",
        "delivery-a",
        "attempt-a",
        "binding-a",
        "yua",
        DeliveryMode.SESSION_BOUND,
    )
    delivery_b = Delivery(
        "event-b",
        "delivery-b",
        "attempt-b",
        "binding-b",
        "aoi",
        DeliveryMode.SESSION_BOUND,
    )
    marker_a = authority.mint(delivery_a, issued_at=ISSUED_AT, expires_at=EXPIRES_AT)
    marker_b = authority.mint(delivery_b, issued_at=ISSUED_AT, expires_at=EXPIRES_AT)

    def wire(seq, name, workspace, *, payload=None):
        frame = event("boot-interleaved", seq, name=name, payload=payload)
        frame["workspace_id"] = workspace
        frame["occurred_at"] = OBSERVED_AT
        return frame

    source_events = [
        wire(1, "surface.input_sent", "workspace-a"),
        wire(2, "surface.input_sent", "workspace-b"),
        wire(
            3,
            "workspace.prompt.submitted",
            "workspace-a",
            payload={"message_preview": authority.encode(marker_a)},
        ),
        wire(
            4,
            "workspace.prompt.submitted",
            "workspace-b",
            payload={"message_preview": authority.encode(marker_b)},
        ),
        wire(
            5,
            "agent.hook.UserPromptSubmit",
            "workspace-a",
            payload={"session_id": "session-a"},
        ),
        wire(
            6,
            "agent.hook.UserPromptSubmit",
            "workspace-b",
            payload={"session_id": "session-b"},
        ),
        wire(7, "agent.hook.Stop", "workspace-a", payload={"session_id": "session-a"}),
        wire(8, "agent.hook.Stop", "workspace-b", payload={"session_id": "session-b"}),
    ]
    socket_path = short_socket_path(tmp_path, "interleaved-receipts")
    config = RuntimeConfig(
        socket_path=socket_path,
        state_dir=tmp_path / "interleaved-state",
        password=None,
        source_instance_id=SOURCE_INSTANCE,
    )
    config.state_dir.mkdir()
    emitted = []
    deliveries = {
        delivery_a.delivery_id: (delivery_a, marker_a),
        delivery_b.delivery_id: (delivery_b, marker_b),
    }
    supervisor = Supervisor.with_receipts(
        config,
        core_client=emitted.append,
        provider_id=PROVIDER_ID,
        delivery_lookup=deliveries.get,
    )
    frames = [
        ack(
            "boot-interleaved",
            replay_count=0,
            gap=False,
            requested_after_seq=None,
            latest_seq=8,
        ),
        *source_events,
    ]

    with CmuxSocketHarness(socket_path, [frames]):
        supervisor.run_once()

    assert {receipt.delivery_id for receipt in emitted} == {
        delivery_a.delivery_id,
        delivery_b.delivery_id,
    }
