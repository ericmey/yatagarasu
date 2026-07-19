"""Honest acceptance coverage for Y-CMUX-006 through Y-CMUX-010.

An acceptance hook is evidence about production behavior.  When the production
seam does not exist yet, the hook is skipped against the issue that must supply
it; this file never reimplements the missing subsystem in a fake just to turn the
suite green.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable

from yatagarasu_cmux import (
    EVENT_INPUT_SENT,
    EVENT_PROMPT_SUBMITTED,
    CmuxSocketTransport,
    EventCursor,
    EventOutbox,
    EventStreamResident,
    HarnessKind,
    Injector,
    NotificationLifecycle,
    SubmitOutcome,
    UnixCmuxSocketClient,
    profile_for,
)
from yatagarasu_cmux.journal import InjectionJournal, JournalState

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

from .socket_harness import (
    CmuxSocketHarness,
    ack,
    event,
    short_socket_path,
    slow_consumer,
)

PROOF_METHOD = "cmux.event_bus.harness_hook_relay"
OBSERVED_AT = "2026-07-18T23:40:00Z"
SIGNING_KEY = b"acceptance-only-signing-key"
MARKER_AUTHORITY = MarkerAuthority(b"acceptance-core-marker-key")
SOURCE_INSTANCE = "cmux-acceptance-resident"


def test_y_cmux_006_slow_consumer_reconnect_has_no_double_injection(
    tmp_path,
) -> None:
    """Reopen SEV-1 if replay causes any delivery to enter a pane twice."""
    socket_path = short_socket_path(tmp_path, "acceptance-006")
    transport = _Transport()
    with InjectionJournal(tmp_path / "injection.sqlite") as journal:
        injector = Injector(
            resolver=_Resolver(),
            transport=transport,
            observer=_Observer((EVENT_INPUT_SENT, EVENT_PROMPT_SUBMITTED)),
            marker_authority=MarkerAuthority(SIGNING_KEY),
            on_effect_pending=lambda delivery_id, _surface: journal.prepare(
                delivery_id=delivery_id,
                binding_id="binding-006",
                seat_id="yua",
                marker="marker-006",
                now=1.0,
            ),
        )
        delivery = Delivery(
            "ev-006",
            "delivery-006",
            "attempt",
            "b-1",
            "yua",
            DeliveryMode.SESSION_BOUND,
        )
        result = injector.deliver(
            "yua",
            delivery,
            "test message",
            "2026-07-19T20:00:00Z",
            "2026-07-19T20:05:00Z",
            harness=HarnessKind.CODEX,
        )
        assert result.outcome is SubmitOutcome.SUBMITTED
        journal.settle(
            delivery_id="delivery-006",
            state=JournalState.INJECTED,
            now=2.0,
            source_events=tuple(result.source_events),
        )

        scripts = [
            [
                ack(
                    "boot-006",
                    replay_count=0,
                    gap=False,
                    requested_after_seq=None,
                    latest_seq=1,
                ),
                event("boot-006", 1, name="surface.input_sent"),
                slow_consumer(5),
            ],
            [
                ack(
                    "boot-006",
                    replay_count=4,
                    gap=False,
                    requested_after_seq=1,
                    latest_seq=5,
                ),
                *(event("boot-006", seq) for seq in range(2, 6)),
            ],
        ]
        with EventOutbox(tmp_path / "event-outbox.sqlite") as outbox:
            with CmuxSocketHarness(socket_path, scripts) as harness:
                run = EventStreamResident(
                    source_instance_id="cmux-resident-006",
                    client=UnixCmuxSocketClient(socket_path),
                    outbox=outbox,
                    marker_key=SIGNING_KEY,
                ).run(max_connections=2)

            durable_cursor = outbox.cursor("cmux-resident-006")
            audit = outbox.audit_rows("cmux-resident-006")
            source_rows = outbox.outbox_rows("cmux-resident-006")

        injection_count_after_replay = len(transport.sent)
        double_injection = (
            {"delivery-006"} if injection_count_after_replay != 1 else set()
        )
        observations = {
            "slow_consumer_received": run.slow_consumer_received,
            "disconnect_at_seq": run.disconnect_at_seq,
            "S_persisted": run.reconnect_after_seq[1],
            "reconnect_at_seq": harness.stream_requests[1]["params"]["after_seq"],
            "replay_event_count": run.replay_event_count,
            "outbox_event_count": len(source_rows),
            "injection_event_count": injection_count_after_replay,
            "double_injection.event_id": double_injection,
            "cursor": durable_cursor,
            "audit_kinds": [row["kind"] for row in audit],
        }

    assert observations == {
        "slow_consumer_received": True,
        "disconnect_at_seq": 5,
        "S_persisted": 1,
        "reconnect_at_seq": 1,
        "replay_event_count": 4,
        "outbox_event_count": 5,
        "injection_event_count": 1,
        "double_injection.event_id": set(),
        "cursor": EventCursor("cmux-resident-006", "boot-006", 5),
        "audit_kinds": ["reconnect_replay"],
    }


def test_y_cmux_007_nonfocused_banner_survives_workspace_visibility(tmp_path) -> None:
    """Reopen SEV-1 if workspace visibility withdraws seat B's banner."""
    socket_path = short_socket_path(tmp_path, "acceptance-007")
    cmux = _NotificationCmux()
    with (
        CmuxSocketHarness(socket_path, [], command_handler=cmux.handle) as harness,
        NotificationLifecycle(
            tmp_path / "notifications.sqlite",
            client=UnixCmuxSocketClient(socket_path),
            suppress_only_focused_surface=True,
            per_seat_cap=4,
            mailbox_ttl_s=3_600,
            clock=lambda: 100.0,
        ) as lifecycle,
    ):
        banner = lifecycle.publish(
            event_id="event-007-b",
            delivery_id="delivery-007-b",
            seat_id="seat-b",
            workspace_id="workspace-shared",
            surface_id="surface-b-nonfocused",
            title="Yatagarasu",
            body="INFO for seat B",
        )

        # Making the shared workspace visible emits no lifecycle evidence.
        # In particular it cannot call notification.dismiss for seat B.
        still_present = cmux.notification(banner.notification_id)
        dismiss_before_receipt = [
            request
            for request in harness.command_requests
            if request["method"] == "notification.dismiss"
        ]
        rejected_cleanup = lifecycle.on_receipt(
            event_id="event-007-b",
            delivery_id="delivery-007-b",
            status="rejected",
            state="in-session",
            evidence_class="harness.prompt_accepted",
        )
        transport_only_cleanup = lifecycle.on_receipt(
            event_id="event-007-b",
            delivery_id="delivery-007-b",
            status="accepted",
            state="transport-submitted",
            evidence_class="harness.prompt_accepted",
        )
        wrong_event_cleanup = lifecycle.on_receipt(
            event_id="event-007-a",
            delivery_id="delivery-007-b",
            status="accepted",
            state="in-session",
            evidence_class="harness.prompt_accepted",
        )
        accepted_cleanup = lifecycle.on_receipt(
            event_id="event-007-b",
            delivery_id="delivery-007-b",
            status="accepted",
            state="in-session",
            evidence_class="harness.prompt_accepted",
        )

    observations = {
        "created": still_present is not None,
        "target_workspace": still_present["workspace_id"],
        "target_surface": still_present["surface_id"],
        "dismiss_before_receipt": len(dismiss_before_receipt),
        "rejected_receipt_dismissed": rejected_cleanup,
        "transport_only_dismissed": transport_only_cleanup,
        "wrong_event_dismissed": wrong_event_cleanup,
        "accepted_in_session_dismissed": accepted_cleanup,
        "remaining_notification_count": len(cmux.notifications),
        "focus_mutation_count": len(
            [
                request
                for request in harness.command_requests
                if ".focus" in str(request["method"])
            ]
        ),
    }
    assert observations == {
        "created": True,
        "target_workspace": "workspace-shared",
        "target_surface": "surface-b-nonfocused",
        "dismiss_before_receipt": 0,
        "rejected_receipt_dismissed": False,
        "transport_only_dismissed": False,
        "wrong_event_dismissed": False,
        "accepted_in_session_dismissed": True,
        "remaining_notification_count": 0,
        "focus_mutation_count": 0,
    }


class _NotificationCmux:
    """Protocol-faithful notification store behind the real Unix harness."""

    def __init__(self) -> None:
        self.notifications: list[dict[str, object]] = []
        self.next_id = 1

    def notification(self, notification_id: str) -> dict[str, object] | None:
        return next(
            (item for item in self.notifications if item["id"] == notification_id),
            None,
        )

    def handle(self, request: dict[str, object]) -> dict[str, object]:
        method = request["method"]
        params = request.get("params")
        assert isinstance(params, dict)
        if method == "notification.create_for_target":
            notification = {
                "id": f"notification-{self.next_id}",
                "workspace_id": params["workspace_id"],
                "surface_id": params["surface_id"],
                "title": params["title"],
                "subtitle": params["subtitle"],
                "body": params["body"],
                "is_read": False,
            }
            self.next_id += 1
            self.notifications.append(notification)
            return {
                "ok": True,
                "result": {
                    "workspace_id": params["workspace_id"],
                    "surface_id": params["surface_id"],
                },
            }
        if method == "notification.list":
            return {"ok": True, "result": {"notifications": self.notifications}}
        if method == "notification.dismiss":
            before = len(self.notifications)
            self.notifications = [
                item for item in self.notifications if item["id"] != params["id"]
            ]
            if len(self.notifications) == before:
                return {
                    "ok": False,
                    "error": {"code": "not_found", "message": "not found"},
                }
            return {"ok": True, "result": {"dismissed": 1}}
        return {
            "ok": False,
            "error": {"code": "method_not_found", "message": str(method)},
        }


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
        self.submitted: list[tuple[str, str]] = []

    def send_text(self, surface: str, text: str) -> None:
        self.sent.append((surface, text))

    def submit(self, surface: str, key: str) -> None:
        self.submitted.append((surface, key))


class _Observer:
    def __init__(self, events: tuple[str, ...] = (EVENT_INPUT_SENT,)) -> None:
        self._events = events

    def observe(self, marker, timeout_s: float) -> Iterable[str]:
        yield from self._events


def test_y_cmux_010_incomplete_busy_submit_holds_and_never_requeues() -> None:
    """Reopen SEV-1 if an ambiguous terminal submit is blindly requeued.

    Once CMUX accepts input but has not emitted prompt-submitted, the result is
    UNKNOWN and held. The plugin never retries by guessing whether the harness
    accepted the next-turn action.
    """
    transport = _Transport()
    injector = Injector(
        resolver=_Resolver(),
        transport=transport,
        observer=_Observer(),
        marker_authority=MarkerAuthority(SIGNING_KEY),
    )

    delivery = Delivery(
        "ev-010", "delivery-010", "attempt", "b-1", "yua", DeliveryMode.SESSION_BOUND
    )
    result = injector.deliver(
        "yua",
        delivery,
        "next turn",
        "2026-07-19T20:00:00Z",
        "2026-07-19T20:05:00Z",
        harness=HarnessKind.CODEX,
    )

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
        "submit_count": 2,
    }


def test_y_cmux_010_busy_codex_send_uses_next_turn_action(tmp_path) -> None:
    """Reopen SEV-1 if busy delivery takes Codex's steer path.

    The real Unix socket seam must receive exactly one text injection followed
    by Codex's explicit queue key. Codex itself owns and drains the next-turn
    queue; Yatagarasu owns selecting ``tab`` rather than busy ``enter``.
    """
    socket_path = short_socket_path(tmp_path, "acceptance-010")
    with CmuxSocketHarness(socket_path, []) as harness:
        transport = CmuxSocketTransport.from_socket_path(socket_path)
        profile = profile_for(HarnessKind.CODEX)
        transport.send_text(
            "00000000-0000-0000-0000-000000000010", profile.render("next turn")
        )
        for key in profile.submit_keys:
            transport.submit("00000000-0000-0000-0000-000000000010", key)

    observations = [
        (request["method"], request["params"]) for request in harness.command_requests
    ]
    assert observations == [
        (
            "surface.send_text",
            {
                "surface_id": "00000000-0000-0000-0000-000000000010",
                "text": "next turn",
            },
        ),
        (
            "surface.send_key",
            {
                "surface_id": "00000000-0000-0000-0000-000000000010",
                "key": "tab",
            },
        ),
        (
            "surface.send_key",
            {
                "surface_id": "00000000-0000-0000-0000-000000000010",
                "key": "enter",
            },
        ),
    ]
