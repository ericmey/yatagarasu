"""Honest acceptance coverage for Y-CMUX-006 through Y-CMUX-010.

An acceptance hook is evidence about production behavior.  When the production
seam does not exist yet, the hook is skipped against the issue that must supply
it; this file never reimplements the missing subsystem in a fake just to turn the
suite green.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable

import pytest
from yatagarasu_cmux import (
    EVENT_INPUT_SENT,
    Injector,
    Marker,
    SubmitOutcome,
)
from yatagarasu_cmux.journal import InjectionJournal

from yatagarasu_core import (
    BroadcastKernel,
    CoreStore,
    CorrelationRule,
    Delivery,
    DeliveryMode,
    Disposition,
    EvidenceClass,
    MarkerAuthority,
    ProofMethodRegistration,
    ProviderKind,
    Receipt,
    ReceiptReducer,
    SessionBinding,
    SessionProof,
    SourceEventRef,
    SourceKind,
)

PROOF_METHOD = "cmux.event_bus.harness_hook_relay"
OBSERVED_AT = "2026-07-18T23:40:00Z"
SIGNING_KEY = b"acceptance-only-signing-key"
MARKER_AUTHORITY = MarkerAuthority(b"acceptance-core-marker-key")
SOURCE_INSTANCE = "cmux-acceptance-resident"


@pytest.mark.skip(
    reason="Y-CMUX-006 requires the production event-stream resident in issue #22"
)
def test_y_cmux_006_slow_consumer_reconnect_has_no_double_injection() -> None:
    """Reopen SEV-1 if replay causes any delivery to enter a pane twice."""


@pytest.mark.skip(
    reason="Y-CMUX-007 requires the production notification lifecycle in issue #23"
)
def test_y_cmux_007_nonfocused_banner_survives_workspace_visibility() -> None:
    """Reopen SEV-1 if workspace visibility withdraws seat B's banner."""


def _delivery() -> Delivery:
    return Delivery(
        event_id="event-008",
        delivery_id="delivery-008",
        attempt_id="attempt-008",
        binding_id="binding-008",
        recipient_id="yua",
        delivery_mode=DeliveryMode.SESSION_BOUND,
    )


def _receipt(
    delivery: Delivery,
    *,
    receipt_id: str,
    provider_id: str,
    evidence_class: EvidenceClass,
    disposition: Disposition | None = None,
    proof: SessionProof | None = None,
    source_event_id: str | None = None,
) -> Receipt:
    return Receipt(
        receipt_id=receipt_id,
        event_id=delivery.event_id,
        delivery_id=delivery.delivery_id,
        attempt_id=delivery.attempt_id,
        binding_id=delivery.binding_id,
        evidence_provider_id=provider_id,
        evidence_class=evidence_class,
        proof_method=PROOF_METHOD,
        observed_at=OBSERVED_AT,
        source_event_id=source_event_id or f"source-{receipt_id}",
        disposition=disposition,
        proof=proof,
    )


def _proof(
    delivery: Delivery,
    evidence: EvidenceClass,
    prompt: SessionProof | None = None,
) -> SessionProof:
    marker = MARKER_AUTHORITY.mint(
        delivery,
        issued_at="2026-07-18T23:39:00Z",
        expires_at="2026-07-18T23:41:00Z",
    )
    if evidence is EvidenceClass.HARNESS_TURN_COMPLETED:
        assert prompt is not None
        events = (
            *prompt.source_events,
            SourceEventRef(
                SOURCE_INSTANCE,
                "boot-acceptance",
                4,
                "source-stop",
                "agent.hook.Stop",
                session_id="session-acceptance",
            ),
        )
    else:
        events = (
            SourceEventRef(
                SOURCE_INSTANCE,
                "boot-acceptance",
                1,
                "source-input",
                "surface.input_sent",
            ),
            SourceEventRef(
                SOURCE_INSTANCE,
                "boot-acceptance",
                2,
                "source-prompt",
                "workspace.prompt.submitted",
                binding_id=delivery.binding_id,
                marker_signature=marker.signature,
            ),
            SourceEventRef(
                SOURCE_INSTANCE,
                "boot-acceptance",
                3,
                "source-hook",
                "agent.hook.UserPromptSubmit",
                session_id="session-acceptance",
            ),
        )
    return SessionProof("session-acceptance", marker, events, turn_id="turn-008")


def _session_reducer() -> tuple[CoreStore, ReceiptReducer, Delivery]:
    store = CoreStore()
    delivery = _delivery()
    store.add_delivery(delivery)
    store.set_dispatching(delivery.delivery_id)
    store.register_provider(
        "cmux-session",
        ProviderKind.SESSION_TRANSPORT,
        {
            EvidenceClass.TRANSPORT_SUBMIT_ACK,
            EvidenceClass.HARNESS_PROMPT_ACCEPTED,
            EvidenceClass.HARNESS_TURN_COMPLETED,
        },
    )
    store.register_session_binding(
        SessionBinding(
            binding_id=delivery.binding_id,
            recipient_id=delivery.recipient_id,
            provider_id="cmux-session",
            adapter_instance_id="adapter-acceptance",
            harness="codex",
            session_id="session-acceptance",
            established_at="2026-07-18T23:00:00Z",
            expires_at="2026-07-19T00:00:00Z",
            proof_methods=(
                ProofMethodRegistration(
                    proof_method=PROOF_METHOD,
                    source_kind=SourceKind.EVENT_BUS,
                    source_instance_id=SOURCE_INSTANCE,
                    correlation_rule=CorrelationRule.CMUX_HARNESS_CHAIN,
                    evidence_classes=frozenset(
                        {
                            EvidenceClass.HARNESS_PROMPT_ACCEPTED,
                            EvidenceClass.HARNESS_TURN_COMPLETED,
                        }
                    ),
                ),
            ),
        )
    )
    return store, ReceiptReducer(store, MARKER_AUTHORITY), delivery


def test_y_cmux_008_existing_evidence_classes_advance_only_linearly() -> None:
    """Reopen SEV-1 if a supported session receipt skips a reducer state.

    This proves the production reducer mapping that exists today.  It does not
    claim the still-missing marker/source-chain validation covered by issue #24.
    """
    store, reducer, delivery = _session_reducer()
    try:
        transport_receipt = _receipt(
            delivery,
            receipt_id="receipt-transport",
            provider_id="cmux-session",
            evidence_class=EvidenceClass.TRANSPORT_SUBMIT_ACK,
        )
        prompt_proof = _proof(delivery, EvidenceClass.HARNESS_PROMPT_ACCEPTED)
        prompt_receipt = _receipt(
            delivery,
            receipt_id="receipt-prompt",
            provider_id="cmux-session",
            evidence_class=EvidenceClass.HARNESS_PROMPT_ACCEPTED,
            proof=prompt_proof,
            source_event_id=prompt_proof.source_events[-1].source_event_id,
        )
        completed_proof = _proof(
            delivery, EvidenceClass.HARNESS_TURN_COMPLETED, prompt_proof
        )
        completed_receipt = _receipt(
            delivery,
            receipt_id="receipt-stop",
            provider_id="cmux-session",
            evidence_class=EvidenceClass.HARNESS_TURN_COMPLETED,
            proof=completed_proof,
            source_event_id=completed_proof.source_events[-1].source_event_id,
        )
        results = [
            reducer.submit(transport_receipt),
            reducer.submit(prompt_receipt),
            reducer.submit(completed_receipt),
        ]
        observations = [
            (
                receipt.evidence_class.value,
                result.state.value if result.state else "none",
                result.disposition.value if result.disposition else None,
            )
            for receipt, result in zip(
                (transport_receipt, prompt_receipt, completed_receipt),
                results,
                strict=True,
            )
        ]

        assert observations == [
            ("transport.submit_ack", "transport-submitted", None),
            ("harness.prompt_accepted", "in-session", None),
            ("harness.turn_completed", "processed", "completed"),
        ]
        assert [
            row["proof_method"] for row in store.audit_for(delivery.delivery_id)
        ] == [
            PROOF_METHOD,
            PROOF_METHOD,
            PROOF_METHOD,
        ]
    finally:
        store.close()


def test_y_cmux_008_comms_view_cannot_issue_session_evidence() -> None:
    """Reopen SEV-1 if a comms-view provider can forge in-session evidence."""
    store, reducer, delivery = _session_reducer()
    try:
        transport = reducer.submit(
            _receipt(
                delivery,
                receipt_id="receipt-transport",
                provider_id="cmux-session",
                evidence_class=EvidenceClass.TRANSPORT_SUBMIT_ACK,
            )
        )
        store.register_provider(
            "discord-view",
            ProviderKind.COMMS_VIEW,
            {EvidenceClass.HARNESS_PROMPT_ACCEPTED},
        )
        forged = reducer.submit(
            _receipt(
                delivery,
                receipt_id="receipt-forged-prompt",
                provider_id="discord-view",
                evidence_class=EvidenceClass.HARNESS_PROMPT_ACCEPTED,
            )
        )

        observations = {
            "transport_state": transport.state.value if transport.state else "none",
            "forged_status": forged.status,
            "forged_reason": forged.reason,
            "durable_state": store.get_delivery(delivery.delivery_id).state.value,
        }
        assert observations == {
            "transport_state": "transport-submitted",
            "forged_status": "rejected",
            "forged_reason": "provider_kind_not_session_transport",
            "durable_state": "transport-submitted",
        }
    finally:
        store.close()


def test_y_cmux_008_turn_end_never_proves_answered() -> None:
    """Reopen SEV-1 if a bare Stop can claim an authored disposition."""
    store, reducer, delivery = _session_reducer()
    try:
        reducer.submit(
            _receipt(
                delivery,
                receipt_id="receipt-transport",
                provider_id="cmux-session",
                evidence_class=EvidenceClass.TRANSPORT_SUBMIT_ACK,
            )
        )
        prompt_proof = _proof(delivery, EvidenceClass.HARNESS_PROMPT_ACCEPTED)
        reducer.submit(
            _receipt(
                delivery,
                receipt_id="receipt-prompt",
                provider_id="cmux-session",
                evidence_class=EvidenceClass.HARNESS_PROMPT_ACCEPTED,
                proof=prompt_proof,
                source_event_id=prompt_proof.source_events[-1].source_event_id,
            )
        )
        result = reducer.submit(
            _receipt(
                delivery,
                receipt_id="receipt-overclaim",
                provider_id="cmux-session",
                evidence_class=EvidenceClass.HARNESS_TURN_COMPLETED,
                disposition=Disposition.ANSWERED,
            )
        )

        observations = {
            "status": result.status,
            "reason": result.reason,
            "durable_state": store.get_delivery(delivery.delivery_id).state.value,
        }
        assert observations == {
            "status": "rejected",
            "reason": "disposition_overclaim",
            "durable_state": "in-session",
        }
    finally:
        store.close()


def test_y_cmux_008_full_marker_binding_and_source_chain_is_required() -> None:
    """Reopen SEV-1 if session_id alone can advance a delivery."""
    store, reducer, delivery = _session_reducer()
    try:
        reducer.submit(
            _receipt(
                delivery,
                receipt_id="receipt-transport",
                provider_id="cmux-session",
                evidence_class=EvidenceClass.TRANSPORT_SUBMIT_ACK,
            )
        )
        session_only = reducer.submit(
            _receipt(
                delivery,
                receipt_id="receipt-session-only",
                provider_id="cmux-session",
                evidence_class=EvidenceClass.HARNESS_PROMPT_ACCEPTED,
                source_event_id="source-hook",
            )
        )

        assert {
            "status": session_only.status,
            "reason": session_only.reason,
            "durable_state": store.get_delivery(delivery.delivery_id).state.value,
            "rejection_audit": store.rejections_for(delivery.delivery_id)[-1]["reason"],
        } == {
            "status": "rejected",
            "reason": "session_proof_required",
            "durable_state": "transport-submitted",
            "rejection_audit": "session_proof_required",
        }
    finally:
        store.close()


def test_y_cmux_009_broadcast_returns_one_literal_outcome_per_seat() -> None:
    """Reopen SEV-1 if a broadcast hides an absent seat behind a rollup."""
    store = CoreStore()
    counts: defaultdict[str, int] = defaultdict(int)

    def next_id(kind: str) -> str:
        counts[kind] += 1
        return f"{kind}-009-{counts[kind]}"

    recipients = ("yua", "aoi", "tama", "shiori", "nyla")
    try:
        store.register_provider(
            "cmux-broadcast",
            ProviderKind.SESSION_TRANSPORT,
            {
                EvidenceClass.TRANSPORT_SUBMIT_ACK,
                EvidenceClass.HARNESS_PROMPT_ACCEPTED,
            },
        )
        for index, recipient_id in enumerate(recipients):
            store.register_session_binding(
                SessionBinding(
                    binding_id=f"binding-009-{recipient_id}",
                    recipient_id=recipient_id,
                    provider_id="cmux-broadcast",
                    adapter_instance_id="cmux-vesper",
                    harness="codex",
                    session_id=f"session-009-{recipient_id}",
                    established_at="2026-07-18T22:00:00Z",
                    expires_at="2026-07-19T00:00:00Z",
                    proof_methods=(
                        ProofMethodRegistration(
                            proof_method=PROOF_METHOD,
                            source_kind=SourceKind.EVENT_BUS,
                            source_instance_id=f"source-009-{index}",
                            correlation_rule=CorrelationRule.CMUX_HARNESS_CHAIN,
                            evidence_classes=frozenset(
                                {EvidenceClass.HARNESS_PROMPT_ACCEPTED}
                            ),
                        ),
                    ),
                )
            )
        store.replace_room_roster("family-009", recipients)
        kernel = BroadcastKernel(store, next_id)
        created = kernel.broadcast(
            actor_id="eric",
            room_id="family-009",
            content="one canonical event",
            accepted_at=OBSERVED_AT,
        )

        store.revoke_session_binding("binding-009-nyla")
        reducer = ReceiptReducer(store)
        for outcome in created.outcomes[:-1]:
            delivery = store.get_delivery(outcome.delivery_id)
            assert delivery is not None and delivery.binding_id is not None
            store.set_dispatching(delivery.delivery_id)
            accepted = reducer.submit(
                Receipt(
                    receipt_id=f"receipt-009-{outcome.recipient_id}",
                    event_id=delivery.event_id,
                    delivery_id=delivery.delivery_id,
                    attempt_id=delivery.attempt_id,
                    binding_id=delivery.binding_id,
                    evidence_provider_id="cmux-broadcast",
                    evidence_class=EvidenceClass.TRANSPORT_SUBMIT_ACK,
                    proof_method="cmux.surface-input-chain",
                    observed_at=OBSERVED_AT,
                    source_event_id=f"source-submit-009-{outcome.recipient_id}",
                )
            )
            assert accepted.state.value == "transport-submitted"

        result = kernel.result(created.broadcast_id)
        audit = store.broadcast_audit(created.broadcast_id)
        observations = {
            "broadcast.outcome_count": len(result.outcomes),
            "outcome.states": {
                outcome.recipient_id: outcome.state.value for outcome in result.outcomes
            },
            "outcome.unavailable": {
                outcome.recipient_id: outcome.unavailable_reason
                for outcome in result.outcomes
                if outcome.unavailable_reason
            },
            "outcome.rollup.all_delivered": result.all_delivered,
            "audit.broadcast_id": audit["broadcast_id"],
            "audit.roster_snapshot_size": audit["roster_snapshot_size"],
        }
    finally:
        store.close()

    assert observations == {
        "broadcast.outcome_count": 5,
        "outcome.states": {
            "yua": "transport-submitted",
            "aoi": "transport-submitted",
            "tama": "transport-submitted",
            "shiori": "transport-submitted",
            "nyla": "queued",
        },
        "outcome.unavailable": {"nyla": "binding-revoked-or-superseded"},
        "outcome.rollup.all_delivered": False,
        "audit.broadcast_id": created.broadcast_id,
        "audit.roster_snapshot_size": 5,
    }


def test_y_cmux_009_journal_preserves_one_delivery_row_per_seat(tmp_path) -> None:
    """Reopen SEV-1 if shared event fan-out collapses recipient deliveries.

    This proves the production journal's implemented part of Y-CMUX-009: five
    recipient delivery IDs remain five durable local-effect records. It does
    not claim the absent core broadcast/outcome matrix tracked by issue #25.
    """
    seat_ids = [f"seat-{index}" for index in range(5)]
    with InjectionJournal(tmp_path / "broadcast.sqlite") as journal:
        for index, seat_id in enumerate(seat_ids):
            journal.prepare(
                delivery_id=f"delivery-broadcast-{index}",
                binding_id=f"binding-{index}",
                seat_id=seat_id,
                marker=f"marker-{index}",
                now=float(index),
            )

        rows = journal.unsettled()
        observations = {
            "row_count": len(rows),
            "delivery_ids": [row.delivery_id for row in rows],
            "seat_ids": [row.seat_id for row in rows],
        }

    assert observations == {
        "row_count": 5,
        "delivery_ids": [f"delivery-broadcast-{index}" for index in range(5)],
        "seat_ids": seat_ids,
    }


class _Resolver:
    def resolve(self, identity: str) -> str:
        return "surface:acceptance"


class _Transport:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []
        self.submitted: list[str] = []

    def send_text(self, surface: str, text: str) -> None:
        self.sent.append((surface, text))

    def submit(self, surface: str) -> None:
        self.submitted.append(surface)


class _Observer:
    def observe(self, marker: Marker, timeout_s: float) -> Iterable[str]:
        yield EVENT_INPUT_SENT


def test_y_cmux_010_incomplete_busy_submit_holds_and_never_requeues() -> None:
    """Reopen SEV-1 if ambiguous admission is blindly requeued.

    The native composer no-clobber behavior remains gated on issue #26.  This
    test proves the implemented injector side of the contract: once CMUX accepts
    input but has not emitted prompt-submitted, the result is UNKNOWN and held.
    """
    transport = _Transport()
    injector = Injector(
        resolver=_Resolver(),
        transport=transport,
        observer=_Observer(),
        signing_key=SIGNING_KEY,
    )

    result = injector.deliver("yua", "delivery-010", "next turn")

    observations = {
        "outcome": result.outcome.value,
        "source_events": list(result.source_events),
        "must_hold": result.must_hold,
        "may_requeue": result.may_requeue,
        "send_count": len(transport.sent),
        "submit_count": len(transport.submitted),
    }
    assert observations == {
        "outcome": SubmitOutcome.UNKNOWN.value,
        "source_events": [EVENT_INPUT_SENT],
        "must_hold": True,
        "may_requeue": False,
        "send_count": 1,
        "submit_count": 1,
    }


@pytest.mark.skip(
    reason="Y-CMUX-010 native composer no-clobber proof requires production issue #26"
)
def test_y_cmux_010_busy_composer_is_unchanged_until_turn_completion() -> None:
    """Reopen SEV-1 if delivery mutates a busy human composer."""
