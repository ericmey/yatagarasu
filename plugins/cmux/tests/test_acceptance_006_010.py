"""Honest acceptance coverage for Y-CMUX-006 through Y-CMUX-010.

An acceptance hook is evidence about production behavior.  When the production
seam does not exist yet, the hook is skipped against the issue that must supply
it; this file never reimplements the missing subsystem in a fake just to turn the
suite green.
"""

from __future__ import annotations

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
    CoreStore,
    Delivery,
    DeliveryMode,
    Disposition,
    EvidenceClass,
    ProviderKind,
    Receipt,
    ReceiptReducer,
)

PROOF_METHOD = "cmux.event_bus.harness_hook_relay"
OBSERVED_AT = "2026-07-18T23:40:00Z"
SIGNING_KEY = b"acceptance-only-signing-key"


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
        source_event_id=f"source-{receipt_id}",
        disposition=disposition,
    )


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
    return store, ReceiptReducer(store), delivery


def test_y_cmux_008_existing_evidence_classes_advance_only_linearly() -> None:
    """Reopen SEV-1 if a supported session receipt skips a reducer state.

    This proves the production reducer mapping that exists today.  It does not
    claim the still-missing marker/source-chain validation covered by issue #24.
    """
    store, reducer, delivery = _session_reducer()
    try:
        observations: list[tuple[str, str, str | None]] = []
        for receipt_id, evidence in (
            ("receipt-transport", EvidenceClass.TRANSPORT_SUBMIT_ACK),
            ("receipt-prompt", EvidenceClass.HARNESS_PROMPT_ACCEPTED),
            ("receipt-stop", EvidenceClass.HARNESS_TURN_COMPLETED),
        ):
            result = reducer.submit(
                _receipt(
                    delivery,
                    receipt_id=receipt_id,
                    provider_id="cmux-session",
                    evidence_class=evidence,
                )
            )
            observations.append(
                (
                    evidence.value,
                    result.state.value if result.state else "none",
                    result.disposition.value if result.disposition else None,
                )
            )

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
        reducer.submit(
            _receipt(
                delivery,
                receipt_id="receipt-prompt",
                provider_id="cmux-session",
                evidence_class=EvidenceClass.HARNESS_PROMPT_ACCEPTED,
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


@pytest.mark.skip(
    reason="Y-CMUX-008 full proof bundle requires production core issue #24"
)
def test_y_cmux_008_full_marker_binding_and_source_chain_is_required() -> None:
    """Reopen SEV-1 if session_id alone can advance a delivery."""


@pytest.mark.skip(
    reason="Y-CMUX-009 requires the production broadcast primitive in issue #25"
)
def test_y_cmux_009_broadcast_returns_one_literal_outcome_per_seat() -> None:
    """Reopen SEV-1 if a broadcast hides an absent seat behind a rollup."""


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
