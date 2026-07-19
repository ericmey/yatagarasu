"""Production contract tests for Issue #25's broadcast primitive."""

from __future__ import annotations

import sqlite3
from collections import defaultdict

import pytest

from yatagarasu_core import (
    BroadcastConflictError,
    BroadcastKernel,
    CoreStore,
    CorrelationRule,
    DeliveryState,
    EvidenceClass,
    ProofMethodRegistration,
    ProviderKind,
    Receipt,
    ReceiptReducer,
    SessionBinding,
    SourceKind,
)

NOW = "2026-07-18T21:00:00Z"
RECIPIENTS = ("yua", "aoi", "tama", "shiori", "nyla")


class SequentialIds:
    def __init__(self) -> None:
        self.counts: defaultdict[str, int] = defaultdict(int)

    def __call__(self, kind: str) -> str:
        self.counts[kind] += 1
        return f"{kind}-{self.counts[kind]}"


def register_bound_roster(store: CoreStore) -> None:
    store.register_provider(
        "cmux-provider",
        ProviderKind.SESSION_TRANSPORT,
        {
            EvidenceClass.TRANSPORT_SUBMIT_ACK,
            EvidenceClass.HARNESS_PROMPT_ACCEPTED,
        },
    )
    for index, recipient_id in enumerate(RECIPIENTS):
        store.register_session_binding(
            SessionBinding(
                binding_id=f"binding-{recipient_id}",
                recipient_id=recipient_id,
                provider_id="cmux-provider",
                adapter_instance_id="cmux-vesper",
                harness="codex",
                session_id=f"session-{recipient_id}",
                established_at="2026-07-18T20:00:00Z",
                expires_at="2026-07-18T22:00:00Z",
                proof_methods=(
                    ProofMethodRegistration(
                        proof_method="cmux.event-bus",
                        source_kind=SourceKind.EVENT_BUS,
                        source_instance_id=f"cmux-source-{index}",
                        correlation_rule=CorrelationRule.CMUX_HARNESS_CHAIN,
                        evidence_classes=frozenset(
                            {EvidenceClass.HARNESS_PROMPT_ACCEPTED}
                        ),
                    ),
                ),
            )
        )


def submit_transport(store: CoreStore, recipient_id: str, delivery_id: str) -> None:
    delivery = store.get_delivery(delivery_id)
    assert delivery is not None and delivery.binding_id is not None
    store.set_dispatching(delivery_id)
    result = ReceiptReducer(store).submit(
        Receipt(
            receipt_id=f"receipt-{recipient_id}",
            event_id=delivery.event_id,
            delivery_id=delivery.delivery_id,
            attempt_id=delivery.attempt_id,
            binding_id=delivery.binding_id,
            evidence_provider_id="cmux-provider",
            evidence_class=EvidenceClass.TRANSPORT_SUBMIT_ACK,
            proof_method="cmux.surface-input-chain",
            observed_at=NOW,
            source_event_id=f"transport-{recipient_id}",
        )
    )
    assert result.state is DeliveryState.TRANSPORT_SUBMITTED


def test_broadcast_freezes_one_event_and_one_delivery_per_recipient() -> None:
    store = CoreStore()
    try:
        register_bound_roster(store)
        store.replace_room_roster("family", RECIPIENTS)
        kernel = BroadcastKernel(store, SequentialIds())

        result = kernel.broadcast(
            actor_id="eric", room_id="family", content="Good evening", accepted_at=NOW
        )

        assert result.roster_snapshot_size == 5
        assert [outcome.recipient_id for outcome in result.outcomes] == list(RECIPIENTS)
        assert len({outcome.delivery_id for outcome in result.outcomes}) == 5
        assert {
            delivery.event_id
            for delivery in store.deliveries_for_event(result.event_id)
        } == {result.event_id}
        assert store.canonical_event(result.event_id) == {
            "event_id": result.event_id,
            "actor_id": "eric",
            "room_id": "family",
            "content": "Good evening",
            "accepted_at": NOW,
            "authority_scope": "conversation",
        }
        assert store.broadcast_audit(result.broadcast_id) == {
            "broadcast_id": result.broadcast_id,
            "event_id": result.event_id,
            "roster_snapshot_size": 5,
        }
    finally:
        store.close()


def test_roster_snapshot_does_not_change_with_live_room_membership() -> None:
    store = CoreStore()
    try:
        register_bound_roster(store)
        store.replace_room_roster("family", RECIPIENTS)
        kernel = BroadcastKernel(store, SequentialIds())
        created = kernel.broadcast(
            actor_id="eric", room_id="family", content="Snapshot me", accepted_at=NOW
        )

        store.replace_room_roster("family", ("yua", "aoi"))
        reread = kernel.result(created.broadcast_id)

        assert reread.roster_snapshot_size == 5
        assert [outcome.recipient_id for outcome in reread.outcomes] == list(RECIPIENTS)
    finally:
        store.close()


def test_revoked_or_absent_binding_remains_visible_and_rollup_stays_false() -> None:
    store = CoreStore()
    try:
        register_bound_roster(store)
        store.replace_room_roster("family", RECIPIENTS)
        kernel = BroadcastKernel(store, SequentialIds())
        created = kernel.broadcast(
            actor_id="eric", room_id="family", content="Five seats", accepted_at=NOW
        )
        store.revoke_session_binding("binding-nyla")
        for outcome in created.outcomes[:-1]:
            submit_transport(store, outcome.recipient_id, outcome.delivery_id)

        result = kernel.result(created.broadcast_id)
        matrix = {
            outcome.recipient_id: (outcome.state.value, outcome.unavailable_reason)
            for outcome in result.outcomes
        }

        assert matrix == {
            "yua": ("transport-submitted", None),
            "aoi": ("transport-submitted", None),
            "tama": ("transport-submitted", None),
            "shiori": ("transport-submitted", None),
            "nyla": ("queued", "binding-revoked-or-superseded"),
        }
        assert result.all_delivered is False
    finally:
        store.close()


def test_all_delivered_turns_true_only_after_every_seat_has_transport_proof() -> None:
    store = CoreStore()
    try:
        register_bound_roster(store)
        store.replace_room_roster("family", RECIPIENTS)
        kernel = BroadcastKernel(store, SequentialIds())
        created = kernel.broadcast(
            actor_id="eric", room_id="family", content="All five", accepted_at=NOW
        )
        for outcome in created.outcomes[:-1]:
            submit_transport(store, outcome.recipient_id, outcome.delivery_id)

        assert kernel.result(created.broadcast_id).all_delivered is False

        final = created.outcomes[-1]
        submit_transport(store, final.recipient_id, final.delivery_id)
        assert kernel.result(created.broadcast_id).all_delivered is True
    finally:
        store.close()


def test_recipient_without_binding_gets_a_real_queued_delivery() -> None:
    store = CoreStore()
    try:
        register_bound_roster(store)
        store.revoke_session_binding("binding-nyla")
        store.replace_room_roster("family", RECIPIENTS)
        result = BroadcastKernel(store, SequentialIds()).broadcast(
            actor_id="eric", room_id="family", content="Still included", accepted_at=NOW
        )

        nyla = result.outcomes[-1]
        assert (
            nyla.recipient_id,
            nyla.binding_id,
            nyla.state,
            nyla.unavailable_reason,
        ) == (
            "nyla",
            None,
            DeliveryState.QUEUED,
            "binding-absent",
        )
        assert store.get_delivery(nyla.delivery_id) is not None
    finally:
        store.close()


def test_expired_active_binding_is_absent_at_snapshot_time() -> None:
    store = CoreStore()
    try:
        register_bound_roster(store)
        store.replace_room_roster("family", RECIPIENTS)
        result = BroadcastKernel(store, SequentialIds()).broadcast(
            actor_id="eric",
            room_id="family",
            content="After the lease",
            accepted_at="2026-07-18T22:00:00Z",
        )

        assert all(outcome.binding_id is None for outcome in result.outcomes)
        assert all(
            outcome.unavailable_reason == "binding-absent"
            for outcome in result.outcomes
        )
    finally:
        store.close()


def test_delivery_id_collision_rolls_back_the_entire_broadcast() -> None:
    store = CoreStore()
    try:
        register_bound_roster(store)
        store.replace_room_roster("family", RECIPIENTS)

        def colliding_ids(kind: str) -> str:
            return "same-delivery" if kind == "delivery" else f"one-{kind}"

        with pytest.raises(BroadcastConflictError, match="collision"):
            BroadcastKernel(store, colliding_ids).broadcast(
                actor_id="eric",
                room_id="family",
                content="Must roll back",
                accepted_at=NOW,
            )

        assert store.canonical_event("one-event") is None
        assert store.broadcast_audit("one-broadcast") is None
        assert store.deliveries_for_event("one-event") == ()
    finally:
        store.close()


def test_result_names_durable_snapshot_inconsistency() -> None:
    store = CoreStore()
    try:
        register_bound_roster(store)
        store.replace_room_roster("family", RECIPIENTS)
        kernel = BroadcastKernel(store, SequentialIds())
        created = kernel.broadcast(
            actor_id="eric",
            room_id="family",
            content="Do not overclaim",
            accepted_at=NOW,
        )
        with store.connection:
            store.connection.execute(
                """DELETE FROM broadcast_recipients
                   WHERE broadcast_id = ? AND recipient_id = ?""",
                (created.broadcast_id, "nyla"),
            )

        with pytest.raises(BroadcastConflictError, match="snapshot is inconsistent"):
            kernel.result(created.broadcast_id)
    finally:
        store.close()


def test_prior_not_null_delivery_schema_migrates_without_losing_state(
    tmp_path,
) -> None:
    database = tmp_path / "prior-core.sqlite"
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        CREATE TABLE deliveries (
            delivery_id TEXT PRIMARY KEY,
            event_id TEXT NOT NULL,
            attempt_id TEXT NOT NULL,
            binding_id TEXT NOT NULL,
            recipient_id TEXT NOT NULL,
            delivery_mode TEXT NOT NULL,
            state TEXT NOT NULL,
            disposition TEXT
        );
        INSERT INTO deliveries VALUES (
            'prior-delivery', 'prior-event', 'prior-attempt', 'prior-binding',
            'yua', 'session-bound', 'queued', NULL
        );
        """
    )
    connection.close()

    store = CoreStore(database)
    try:
        binding_column = next(
            row
            for row in store.connection.execute("PRAGMA table_info(deliveries)")
            if row["name"] == "binding_id"
        )
        assert binding_column["notnull"] == 0
        assert store.get_delivery("prior-delivery").binding_id == "prior-binding"
        assert store.connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert store.connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        for child_table in (
            "platform_message_bindings",
            "receipts",
            "audit_log",
            "broadcast_recipients",
        ):
            referenced_tables = {
                row["table"]
                for row in store.connection.execute(
                    f"PRAGMA foreign_key_list({child_table})"
                )
            }
            assert "deliveries" in referenced_tables
            assert "deliveries_legacy_not_null" not in referenced_tables

        store.replace_room_roster("migration-room", ("unbound-seat",))
        result = BroadcastKernel(store, SequentialIds()).broadcast(
            actor_id="eric",
            room_id="migration-room",
            content="Nullable after migration",
            accepted_at=NOW,
        )
        assert result.outcomes[0].binding_id is None
        assert result.outcomes[0].state is DeliveryState.QUEUED
    finally:
        store.close()
