"""The cross-module test for the injector -> reducer seam (#50, from audit #48).

This file exists because of what the audit found: both halves of this boundary
were fully green and nothing ran across it. ``SubmitResult`` was asserted on by
cmux tests; ``Receipt`` was built by hand in core tests, with explicit fields,
bypassing the injector entirely. Two green suites that never touch.

So the rule for everything below: **it must run the real ``ReceiptReducer``
against a real ``CoreStore``.** A test that asserts on the shape of the returned
``Receipt`` would be the same mistake one layer up — it would pass just as
happily against a receipt the reducer rejects.
"""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from yatagarasu_cmux.outcome import SubmitOutcome, SubmitResult
from yatagarasu_cmux.receipt_translation import PROOF_METHOD, submit_ack_receipt

from yatagarasu_core import (
    CoreStore,
    Delivery,
    DeliveryMode,
    DeliveryState,
    Disposition,
    EvidenceClass,
    ProviderKind,
    ReceiptReducer,
)

NOW = "2026-07-19T14:00:00Z"
PROVIDER_ID = "cmux-transport-test"


def _delivery(suffix: str = "1") -> Delivery:
    return Delivery(
        event_id=f"event-{suffix}",
        delivery_id=f"delivery-{suffix}",
        attempt_id=f"attempt-{suffix}",
        binding_id=f"binding-{suffix}",
        recipient_id="recipient-a",
        delivery_mode=DeliveryMode.SESSION_BOUND,
    )


class SubmitToReceiptSeam(unittest.TestCase):
    """Run the injector's output through the core reducer, for real."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = CoreStore(Path(self._tmp.name) / "core.sqlite")
        self.addCleanup(self.store.close)
        self.reducer = ReceiptReducer(self.store)
        self.store.register_provider(
            provider_id=PROVIDER_ID,
            kind=ProviderKind.SESSION_TRANSPORT,
            evidence_classes=frozenset({EvidenceClass.TRANSPORT_SUBMIT_ACK}),
        )

    def _dispatching(self, item: Delivery) -> None:
        """Put the delivery in the only state a submit ack may advance from."""
        self.store.add_delivery(item)
        self.store.set_dispatching(item.delivery_id)

    def test_a_proven_submit_is_accepted_by_the_real_reducer(self) -> None:
        """The assertion the audit found missing: not that a Receipt was built,
        but that the reducer ACCEPTS it and moves the delivery."""
        item = _delivery()
        self._dispatching(item)
        result = SubmitResult(
            outcome=SubmitOutcome.SUBMITTED,
            delivery_id=item.delivery_id,
            source_events=("surface.input_sent", "workspace.prompt.submitted"),
        )

        receipt = submit_ack_receipt(
            result,
            item,
            evidence_provider_id=PROVIDER_ID,
            observed_at=NOW,
            receipt_id="rec-1",
        )
        assert receipt is not None
        outcome = self.reducer.submit(receipt)

        self.assertEqual(outcome.status, "accepted", outcome.reason)
        self.assertEqual(outcome.state, DeliveryState.TRANSPORT_SUBMITTED)

    def test_an_unknown_outcome_produces_no_receipt(self) -> None:
        """The load-bearing negative. UNKNOWN means the send may have landed and
        we did not see it. Emitting a submit ack here would advance the delivery
        out of dispatching on a guess — asserting evidence we never observed."""
        item = _delivery()
        result = SubmitResult(
            outcome=SubmitOutcome.UNKNOWN,
            delivery_id=item.delivery_id,
            detail="socket closed before ack",
        )

        self.assertIsNone(
            submit_ack_receipt(
                result,
                item,
                evidence_provider_id=PROVIDER_ID,
                observed_at=NOW,
                receipt_id="rec-unknown",
            )
        )

    def test_a_clean_negative_produces_no_receipt(self) -> None:
        """NOT_SUBMITTED is proven not-landed and safe to requeue. The delivery
        staying in dispatching IS the record; a receipt would contradict it."""
        item = _delivery()
        result = SubmitResult(
            outcome=SubmitOutcome.NOT_SUBMITTED,
            delivery_id=item.delivery_id,
            detail="target surface not found",
        )

        self.assertIsNone(
            submit_ack_receipt(
                result,
                item,
                evidence_provider_id=PROVIDER_ID,
                observed_at=NOW,
                receipt_id="rec-negative",
            )
        )

    def test_mismatched_delivery_ids_raise_rather_than_correlate(self) -> None:
        """Attributing one delivery's evidence to another is the failure the
        whole receipt model exists to prevent. It must be loud, not silent."""
        result = SubmitResult(
            outcome=SubmitOutcome.SUBMITTED, delivery_id="delivery-OTHER"
        )

        with self.assertRaisesRegex(ValueError, "attribute one delivery"):
            submit_ack_receipt(
                result,
                _delivery(),
                evidence_provider_id=PROVIDER_ID,
                observed_at=NOW,
                receipt_id="rec-x",
            )

    def test_a_submit_ack_carrying_a_disposition_is_rejected_by_the_reducer(
        self,
    ) -> None:
        """Guards the reason the translator sets disposition=None. Acknowledging
        a submit says nothing about how the turn ended, and the reducer enforces
        that — this test proves we are on the right side of its rule rather than
        merely believing we are."""
        item = _delivery("2")
        self._dispatching(item)
        receipt = submit_ack_receipt(
            SubmitResult(SubmitOutcome.SUBMITTED, item.delivery_id),
            item,
            evidence_provider_id=PROVIDER_ID,
            observed_at=NOW,
            receipt_id="rec-2",
        )
        assert receipt is not None

        outcome = self.reducer.submit(
            replace(receipt, disposition=Disposition.COMPLETED)
        )

        self.assertEqual(outcome.status, "rejected")
        self.assertEqual(outcome.reason, "disposition_not_allowed")

    def test_the_proof_method_is_the_one_the_receipt_declares(self) -> None:
        """A translator that named a proof method the provider never registered
        would be rejected downstream for a reason unrelated to the send."""
        item = _delivery("3")
        receipt = submit_ack_receipt(
            SubmitResult(SubmitOutcome.SUBMITTED, item.delivery_id),
            item,
            evidence_provider_id=PROVIDER_ID,
            observed_at=NOW,
            receipt_id="rec-3",
        )
        assert receipt is not None

        self.assertEqual(receipt.proof_method, PROOF_METHOD)
        self.assertIsNone(receipt.proof)


if __name__ == "__main__":
    unittest.main()
