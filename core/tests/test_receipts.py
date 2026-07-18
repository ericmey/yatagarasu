"""Contract tests for Issue #2's two reducer paths."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from yatagarasu_core import (
    CoreStore,
    Delivery,
    DeliveryMode,
    DeliveryState,
    Disposition,
    EvidenceClass,
    ProviderKind,
    Receipt,
    ReceiptReducer,
)
from yatagarasu_core.store import ConcurrentTransitionError

NOW = "2026-07-18T21:00:00Z"


def delivery(mode: DeliveryMode, suffix: str = "1") -> Delivery:
    return Delivery(
        event_id=f"event-{suffix}",
        delivery_id=f"delivery-{suffix}",
        attempt_id=f"attempt-{suffix}",
        binding_id=f"binding-{suffix}",
        recipient_id="recipient-a",
        delivery_mode=mode,
    )


def receipt_for(
    item: Delivery,
    receipt_id: str,
    evidence: EvidenceClass,
    provider_id: str,
    **changes,
) -> Receipt:
    base = Receipt(
        receipt_id=receipt_id,
        event_id=item.event_id,
        delivery_id=item.delivery_id,
        attempt_id=item.attempt_id,
        binding_id=item.binding_id,
        evidence_provider_id=provider_id,
        evidence_class=evidence,
        proof_method="contract.fake",
        observed_at=NOW,
    )
    return replace(base, **changes)


class FakeCommsView:
    def __init__(self, store: CoreStore, item: Delivery) -> None:
        self.store = store
        self.item = item
        self.provider_id = f"fake-comms-{item.delivery_id}"
        store.register_provider(
            self.provider_id,
            ProviderKind.COMMS_VIEW,
            {
                EvidenceClass.TRANSPORT_SUBMIT_ACK,
                EvidenceClass.PARTICIPANT_REPLY_AUTHORED,
                EvidenceClass.PARTICIPANT_REACTION_AUTHORED,
            },
        )
        store.bind_principal(self.provider_id, "platform-user-a", item.recipient_id)
        store.bind_platform_message(
            self.provider_id,
            "platform-message-a",
            item.event_id,
            item.delivery_id,
            item.attempt_id,
        )

    def transport_ack(self) -> Receipt:
        return receipt_for(
            self.item,
            f"receipt-transport-{self.item.delivery_id}",
            EvidenceClass.TRANSPORT_SUBMIT_ACK,
            self.provider_id,
            platform_message_id="platform-message-a",
        )

    def authored(self, evidence: EvidenceClass, disposition=None, source="source-1"):
        return receipt_for(
            self.item,
            f"receipt-{source}",
            evidence,
            self.provider_id,
            source_event_id=source,
            platform_principal_id="platform-user-a",
            platform_message_id="platform-message-a",
            disposition=disposition,
        )


class IntegrityRaceStore(CoreStore):
    """Simulates another writer committing immediately before our insert."""

    winner_transform = None

    def accept_receipt(self, *, receipt, delivery, next_state, disposition):
        if self.winner_transform is None:
            return super().accept_receipt(
                receipt=receipt,
                delivery=delivery,
                next_state=next_state,
                disposition=disposition,
            )
        winner_transform = self.winner_transform
        self.winner_transform = None
        super().accept_receipt(
            receipt=winner_transform(receipt),
            delivery=delivery,
            next_state=next_state,
            disposition=disposition,
        )
        raise sqlite3.IntegrityError("simulated concurrent uniqueness collision")


class ReceiptContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = CoreStore()
        self.reducer = ReceiptReducer(self.store)

    def tearDown(self) -> None:
        self.store.close()

    def test_delivery_mode_is_durable_and_required(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "core.db"
            first = CoreStore(path)
            item = delivery(DeliveryMode.CHANNEL_NATIVE)
            first.add_delivery(item)
            first.close()

            reopened = CoreStore(path)
            self.assertEqual(
                reopened.get_delivery(item.delivery_id).delivery_mode,
                DeliveryMode.CHANNEL_NATIVE,
            )
            with self.assertRaises(sqlite3.IntegrityError):
                reopened.connection.execute(
                    """INSERT INTO deliveries
                       (delivery_id,event_id,attempt_id,binding_id,recipient_id,state)
                       VALUES ('bad','e','a','b','r','queued')"""
                )
            reopened.close()

    def test_session_bound_path_is_strictly_linear(self):
        item = delivery(DeliveryMode.SESSION_BOUND)
        self.store.add_delivery(item)
        self.store.set_dispatching(item.delivery_id)
        self.store.register_provider(
            "fake-session",
            ProviderKind.SESSION_TRANSPORT,
            {
                EvidenceClass.TRANSPORT_SUBMIT_ACK,
                EvidenceClass.HARNESS_PROMPT_ACCEPTED,
                EvidenceClass.HARNESS_TURN_COMPLETED,
            },
        )

        premature = self.reducer.submit(
            receipt_for(
                item,
                "premature",
                EvidenceClass.HARNESS_TURN_COMPLETED,
                "fake-session",
            )
        )
        self.assertEqual(
            (premature.status, premature.reason), ("rejected", "invalid_transition")
        )

        states = []
        for rid, evidence in (
            ("r-transport", EvidenceClass.TRANSPORT_SUBMIT_ACK),
            ("r-session", EvidenceClass.HARNESS_PROMPT_ACCEPTED),
            ("r-complete", EvidenceClass.HARNESS_TURN_COMPLETED),
        ):
            result = self.reducer.submit(
                receipt_for(item, rid, evidence, "fake-session")
            )
            states.append(result.state)
        self.assertEqual(
            states,
            [
                DeliveryState.TRANSPORT_SUBMITTED,
                DeliveryState.IN_SESSION,
                DeliveryState.PROCESSED,
            ],
        )
        self.assertEqual(
            self.store.get_delivery(item.delivery_id).disposition, Disposition.COMPLETED
        )

    def _channel_ready(self, suffix="1"):
        item = delivery(DeliveryMode.CHANNEL_NATIVE, suffix)
        self.store.add_delivery(item)
        self.store.set_dispatching(item.delivery_id)
        fake = FakeCommsView(self.store, item)
        result = self.reducer.submit(fake.transport_ack())
        self.assertEqual(result.state, DeliveryState.TRANSPORT_SUBMITTED)
        return item, fake

    def test_channel_native_reply_skips_in_session_and_audits_gap(self):
        item, fake = self._channel_ready()
        result = self.reducer.submit(
            fake.authored(EvidenceClass.PARTICIPANT_REPLY_AUTHORED)
        )
        self.assertEqual(
            (result.state, result.disposition),
            (DeliveryState.PROCESSED, Disposition.ANSWERED),
        )
        audit = self.store.audit_for(item.delivery_id)
        self.assertEqual(
            [row["to_state"] for row in audit], ["transport-submitted", "processed"]
        )
        self.assertNotIn("in-session", [row["to_state"] for row in audit])
        self.assertEqual(audit[-1]["delivery_mode"], "channel-native")
        self.assertEqual(audit[-1]["session_entry"], "not_applicable")
        self.assertEqual(audit[-1]["evidence_class"], "participant.reply_authored")
        self.assertEqual(audit[-1]["proof_method"], "contract.fake")

    def test_exact_duplicate_is_idempotent_but_contradictory_id_is_rejected(self):
        item, fake = self._channel_ready()
        authored = fake.authored(EvidenceClass.PARTICIPANT_REPLY_AUTHORED)
        self.assertEqual(self.reducer.submit(authored).status, "accepted")
        self.assertEqual(self.reducer.submit(authored).status, "duplicate")

        contradictions = (
            replace(authored, disposition=Disposition.HELD),
            replace(authored, platform_principal_id="other-principal"),
            replace(authored, platform_message_id="other-message"),
            replace(authored, authored_by_provider=True),
            replace(authored, infrastructure_event=True),
        )
        for contradiction in contradictions:
            with self.subTest(contradiction=contradiction):
                result = self.reducer.submit(contradiction)
                self.assertEqual(
                    (result.status, result.reason),
                    ("rejected", "receipt_id_contradiction"),
                )
        self.assertEqual(len(self.store.audit_for(item.delivery_id)), 2)

    def test_session_evidence_rejects_disposition_claims_it_cannot_prove(self):
        item = delivery(DeliveryMode.SESSION_BOUND)
        self.store.add_delivery(item)
        self.store.set_dispatching(item.delivery_id)
        self.store.register_provider(
            "session-dispositions",
            ProviderKind.SESSION_TRANSPORT,
            {
                EvidenceClass.TRANSPORT_SUBMIT_ACK,
                EvidenceClass.HARNESS_PROMPT_ACCEPTED,
                EvidenceClass.HARNESS_TURN_COMPLETED,
            },
        )

        invalid_ack = receipt_for(
            item,
            "invalid-ack-disposition",
            EvidenceClass.TRANSPORT_SUBMIT_ACK,
            "session-dispositions",
            disposition=Disposition.ANSWERED,
        )
        self.assertEqual(
            self.reducer.submit(invalid_ack).reason, "disposition_not_allowed"
        )
        self.reducer.submit(
            receipt_for(
                item,
                "valid-ack",
                EvidenceClass.TRANSPORT_SUBMIT_ACK,
                "session-dispositions",
            )
        )

        invalid_prompt = receipt_for(
            item,
            "invalid-prompt-disposition",
            EvidenceClass.HARNESS_PROMPT_ACCEPTED,
            "session-dispositions",
            disposition=Disposition.ANSWERED,
        )
        self.assertEqual(
            self.reducer.submit(invalid_prompt).reason, "disposition_not_allowed"
        )
        self.reducer.submit(
            receipt_for(
                item,
                "valid-prompt",
                EvidenceClass.HARNESS_PROMPT_ACCEPTED,
                "session-dispositions",
            )
        )

        invalid_completed = receipt_for(
            item,
            "invalid-completed-disposition",
            EvidenceClass.HARNESS_TURN_COMPLETED,
            "session-dispositions",
            disposition=Disposition.ANSWERED,
        )
        self.assertEqual(
            self.reducer.submit(invalid_completed).reason, "disposition_overclaim"
        )
        completed = self.reducer.submit(
            receipt_for(
                item,
                "valid-completed",
                EvidenceClass.HARNESS_TURN_COMPLETED,
                "session-dispositions",
                disposition=Disposition.COMPLETED,
            )
        )
        self.assertEqual(completed.disposition, Disposition.COMPLETED)

    def test_receipt_commit_rejects_a_stale_validated_state_atomically(self):
        item = delivery(DeliveryMode.SESSION_BOUND)
        self.store.add_delivery(item)
        self.store.set_dispatching(item.delivery_id)
        self.store.register_provider(
            "session-race",
            ProviderKind.SESSION_TRANSPORT,
            {EvidenceClass.TRANSPORT_SUBMIT_ACK},
        )
        stale = self.store.get_delivery(item.delivery_id)
        ack = receipt_for(
            item,
            "stale-state-receipt",
            EvidenceClass.TRANSPORT_SUBMIT_ACK,
            "session-race",
        )
        self.store.connection.execute(
            "UPDATE deliveries SET state = ? WHERE delivery_id = ?",
            (DeliveryState.TRANSPORT_SUBMITTED.value, item.delivery_id),
        )
        self.store.connection.commit()

        with self.assertRaises(ConcurrentTransitionError):
            self.store.accept_receipt(
                receipt=ack,
                delivery=stale,
                next_state=DeliveryState.TRANSPORT_SUBMITTED,
                disposition=None,
            )
        self.assertFalse(self.store.receipt_exists(ack.receipt_id))
        self.assertEqual(self.store.audit_for(item.delivery_id), [])

    def test_receipt_and_audit_foreign_keys_preserve_durable_ownership(self):
        receipt_fks = {
            (row[3], row[2])
            for row in self.store.connection.execute(
                "PRAGMA foreign_key_list(receipts)"
            )
        }
        audit_fks = {
            (row[3], row[2])
            for row in self.store.connection.execute(
                "PRAGMA foreign_key_list(audit_log)"
            )
        }
        self.assertIn(("provider_id", "providers"), receipt_fks)
        self.assertIn(("delivery_id", "deliveries"), audit_fks)

    def _use_integrity_race_store(self):
        self.store.close()
        self.store = IntegrityRaceStore()
        self.reducer = ReceiptReducer(self.store)
        return self._channel_ready()

    def test_concurrent_same_receipt_id_returns_duplicate(self):
        _, fake = self._use_integrity_race_store()
        self.store.winner_transform = lambda receipt: receipt
        result = self.reducer.submit(
            fake.authored(EvidenceClass.PARTICIPANT_REPLY_AUTHORED)
        )
        self.assertEqual(result.status, "duplicate")

    def test_concurrent_changed_receipt_id_claim_returns_contradiction(self):
        _, fake = self._use_integrity_race_store()
        self.store.winner_transform = lambda receipt: replace(
            receipt, platform_principal_id="different-principal"
        )
        result = self.reducer.submit(
            fake.authored(EvidenceClass.PARTICIPANT_REPLY_AUTHORED)
        )
        self.assertEqual(
            (result.status, result.reason),
            ("rejected", "receipt_id_contradiction"),
        )

    def test_concurrent_source_event_winner_returns_replayed(self):
        _, fake = self._use_integrity_race_store()
        self.store.winner_transform = lambda receipt: replace(
            receipt, receipt_id="winning-receipt"
        )
        result = self.reducer.submit(
            fake.authored(EvidenceClass.PARTICIPANT_REPLY_AUTHORED)
        )
        self.assertEqual(
            (result.status, result.reason),
            ("rejected", "source_event_replayed"),
        )

    def test_channel_native_reactions_are_acknowledged_or_held_only(self):
        for index, disposition in enumerate(
            (Disposition.ACKNOWLEDGED, Disposition.HELD), start=2
        ):
            with self.subTest(disposition=disposition):
                _, fake = self._channel_ready(str(index))
                result = self.reducer.submit(
                    fake.authored(
                        EvidenceClass.PARTICIPANT_REACTION_AUTHORED,
                        disposition,
                        source=f"source-{index}",
                    )
                )
                self.assertEqual(result.disposition, disposition)

    def test_channel_native_rejects_untrusted_or_inexact_ingress(self):
        item, fake = self._channel_ready()
        valid = fake.authored(EvidenceClass.PARTICIPANT_REPLY_AUTHORED)
        cases = {
            "principal_mismatch": replace(
                valid, receipt_id="wrong-user", platform_principal_id="other"
            ),
            "platform_message_binding_mismatch": replace(
                valid, receipt_id="wrong-message", platform_message_id="other"
            ),
            "self_echo_or_infrastructure_event": replace(
                valid, receipt_id="self-echo", authored_by_provider=True
            ),
            "source_event_id_required": replace(
                valid, receipt_id="no-source", source_event_id=None
            ),
        }
        for reason, forged in cases.items():
            with self.subTest(reason=reason):
                result = self.reducer.submit(forged)
                self.assertEqual((result.status, result.reason), ("rejected", reason))
                self.assertEqual(
                    self.store.get_delivery(item.delivery_id).state,
                    DeliveryState.TRANSPORT_SUBMITTED,
                )

    def test_required_audit_fields_cannot_be_omitted(self):
        _, fake = self._channel_ready()
        missing_proof_method = replace(
            fake.authored(EvidenceClass.PARTICIPANT_REPLY_AUTHORED),
            proof_method="",
        )
        result = self.reducer.submit(missing_proof_method)
        self.assertEqual(
            (result.status, result.reason),
            ("rejected", "audit_fields_required"),
        )
        columns = {
            row[1]: row[3]
            for row in self.store.connection.execute("PRAGMA table_info(audit_log)")
        }
        self.assertEqual(columns["delivery_mode"], 1)
        self.assertEqual(columns["session_entry"], 1)

    def test_source_event_replay_cannot_advance_a_second_delivery(self):
        _, fake = self._channel_ready("1")
        accepted = self.reducer.submit(
            fake.authored(EvidenceClass.PARTICIPANT_REPLY_AUTHORED)
        )
        self.assertEqual(accepted.status, "accepted")

        second = delivery(DeliveryMode.CHANNEL_NATIVE, "2")
        self.store.add_delivery(second)
        self.store.set_dispatching(second.delivery_id)
        self.store.bind_principal(
            fake.provider_id, "platform-user-b", second.recipient_id
        )
        self.store.bind_platform_message(
            fake.provider_id,
            "platform-message-b",
            second.event_id,
            second.delivery_id,
            second.attempt_id,
        )
        self.reducer.submit(
            receipt_for(
                second,
                "second-transport",
                EvidenceClass.TRANSPORT_SUBMIT_ACK,
                fake.provider_id,
                platform_message_id="platform-message-b",
            )
        )
        replay = receipt_for(
            second,
            "replayed-receipt",
            EvidenceClass.PARTICIPANT_REPLY_AUTHORED,
            fake.provider_id,
            source_event_id="source-1",
            platform_principal_id="platform-user-b",
            platform_message_id="platform-message-b",
        )
        result = self.reducer.submit(replay)
        self.assertEqual(
            (result.status, result.reason), ("rejected", "source_event_replayed")
        )

    def test_participant_classes_are_rejected_on_session_bound_delivery(self):
        item = delivery(DeliveryMode.SESSION_BOUND)
        self.store.add_delivery(item)
        self.store.set_dispatching(item.delivery_id)
        self.store.register_provider(
            "comms",
            ProviderKind.COMMS_VIEW,
            {EvidenceClass.PARTICIPANT_REPLY_AUTHORED},
        )
        forged = receipt_for(
            item,
            "forged",
            EvidenceClass.PARTICIPANT_REPLY_AUTHORED,
            "comms",
        )
        result = self.reducer.submit(forged)
        self.assertEqual(result.reason, "evidence_class_wrong_delivery_mode")

    def test_comms_view_cannot_advance_session_bound_transport(self):
        item = delivery(DeliveryMode.SESSION_BOUND)
        self.store.add_delivery(item)
        self.store.set_dispatching(item.delivery_id)
        self.store.register_provider(
            "wrong-kind",
            ProviderKind.COMMS_VIEW,
            {EvidenceClass.TRANSPORT_SUBMIT_ACK},
        )
        result = self.reducer.submit(
            receipt_for(
                item,
                "wrong-kind-receipt",
                EvidenceClass.TRANSPORT_SUBMIT_ACK,
                "wrong-kind",
            )
        )
        self.assertEqual(
            (result.status, result.reason),
            ("rejected", "provider_kind_not_session_transport"),
        )


if __name__ == "__main__":
    unittest.main()
