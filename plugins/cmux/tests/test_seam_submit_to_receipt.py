"""The cross-module test for the injector -> reducer seam (#50, from audit #48).

This file exists because of what the audit found: both halves of this boundary
were fully green and nothing ran across it. ``SubmitResult`` was asserted on by
cmux tests; ``Receipt`` was built by hand in core tests, with explicit fields,
bypassing the injector entirely. Two green suites that never touch.

So the rule for everything below, per Tama's acceptance criterion on #50: the
test drives the **real** ``Injector.deliver`` to produce the ``SubmitResult``,
and feeds the translation through the **real** ``ReceiptReducer`` against a
**real** ``CoreStore``. Nothing hand-builds a ``SubmitResult`` and nothing
hand-builds a ``Receipt``.

The first draft of this file did hand-build the ``SubmitResult``. That met the
letter of "cross-module" while substituting a fixture for the producer — which
is the same failure the audit found, one layer up. Tama caught it.
"""

from __future__ import annotations

import tempfile
import unittest
from collections.abc import Iterable
from dataclasses import replace
from pathlib import Path

from yatagarasu_cmux import (
    EVENT_INPUT_SENT,
    EVENT_PROMPT_SUBMITTED,
    Injector,
    Marker,
    SubmitOutcome,
)
from yatagarasu_cmux.harness_profiles import HarnessKind
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
from yatagarasu_core.proofs import MarkerAuthority

NOW = "2026-07-19T14:00:00Z"
PROVIDER_ID = "cmux-transport-test"
SIGNING_KEY = b"seam-test-signing-key"
ISSUED_AT = "2026-07-19T13:59:00Z"
EXPIRES_AT = "2026-07-19T14:01:00Z"


class _Resolver:
    def resolve(self, identity: str) -> str:
        return "surface:seam"


class _Transport:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []
        self.submitted: list[tuple[str, str]] = []

    def send_text(self, surface: str, text: str) -> None:
        self.sent.append((surface, text))

    def submit(self, surface: str, submit_key: str) -> None:
        # Two args since #56: the submit key is per-harness (Enter / Tab /
        # slash-queue). Keeping the old one-arg stub did not fail loudly — the
        # TypeError was swallowed by deliver()'s `except Exception` and returned
        # as UNKNOWN "transport error", so six tests failed with a wrong-outcome
        # assertion instead of a signature error.
        self.submitted.append((surface, submit_key))


class _Observer:
    """Scripted bus, exactly as the injector's own acceptance tests use.

    Which events it yields is what selects the outcome, so these three scripts
    are the whole reason this file can test all three branches through the real
    producer rather than by constructing the answer:

    - both events      -> SUBMITTED
    - input_sent only  -> UNKNOWN   (busy pane: something happened, we did not
                                     see the submit)
    - nothing          -> NOT_SUBMITTED (clean negative)
    """

    def __init__(self, events: list[str]) -> None:
        self._events = list(events)

    def observe(self, marker: Marker, timeout_s: float) -> Iterable[str]:
        while self._events:
            yield self._events.pop(0)


def _injector(events: list[str]) -> Injector:
    return Injector(
        resolver=_Resolver(),
        transport=_Transport(),
        observer=_Observer(events),
        marker_authority=MarkerAuthority(SIGNING_KEY),
        submit_timeout_s=0.05,
    )


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

    def _deliver(self, item: Delivery, events: list[str]):
        """Run the real injector end to end and return its SubmitResult.

        The signature changed in #47: the injector mints through
        ``MarkerAuthority`` and so needs the ``Delivery`` record rather than a
        bare id. The assertions below are unchanged — only the call moved.
        """
        return _injector(events).deliver(
            "peer",
            item,
            "payload body",
            ISSUED_AT,
            EXPIRES_AT,
            harness=HarnessKind.CLAUDE_CODE,
        )

    def test_submitted_crosses_the_seam_and_commits(self) -> None:
        """Red-proof (a). Both bus events observed -> the real injector reports
        SUBMITTED -> the real reducer ACCEPTS and advances the delivery.

        The load-bearing assertion is the reducer's verdict, not the receipt's
        shape: a shape check passes just as happily against a receipt the
        reducer rejects."""
        item = _delivery()
        self._dispatching(item)

        result = self._deliver(item, [EVENT_INPUT_SENT, EVENT_PROMPT_SUBMITTED])
        self.assertIs(result.outcome, SubmitOutcome.SUBMITTED)
        self.assertTrue(result.source_events, "producer must carry its evidence")

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

    def test_unknown_holds_the_delivery_in_dispatching(self) -> None:
        """Red-proof (c), and the busy-pane regression case.

        Only ``input_sent`` observed: the injector reports UNKNOWN — something
        happened and we did not see the submit. No receipt is emitted, and the
        proof that this is *hold* rather than *clean negative* is that the
        delivery is still in ``dispatching`` afterwards, so nothing downstream
        may requeue it. Emitting a submit ack here would advance the delivery on
        a guess."""
        item = _delivery()
        self._dispatching(item)

        result = self._deliver(item, [EVENT_INPUT_SENT])
        self.assertIs(result.outcome, SubmitOutcome.UNKNOWN)
        self.assertTrue(result.must_hold)
        self.assertFalse(result.may_requeue)

        self.assertIsNone(
            submit_ack_receipt(
                result,
                item,
                evidence_provider_id=PROVIDER_ID,
                observed_at=NOW,
                receipt_id="rec-unknown",
            )
        )
        stored = self.store.get_delivery(item.delivery_id)
        assert stored is not None
        self.assertIs(stored.state, DeliveryState.DISPATCHING)

    def test_a_clean_negative_leaves_the_delivery_requeueable(self) -> None:
        """Red-proof (b). No bus events at all -> NOT_SUBMITTED, a proven
        negative. No receipt: the delivery staying in ``dispatching`` IS the
        record, and unlike UNKNOWN it is safe to requeue."""
        item = _delivery()
        self._dispatching(item)

        result = self._deliver(item, [])
        self.assertIs(result.outcome, SubmitOutcome.NOT_SUBMITTED)
        self.assertTrue(result.may_requeue)

        self.assertIsNone(
            submit_ack_receipt(
                result,
                item,
                evidence_provider_id=PROVIDER_ID,
                observed_at=NOW,
                receipt_id="rec-negative",
            )
        )
        stored = self.store.get_delivery(item.delivery_id)
        assert stored is not None
        self.assertIs(stored.state, DeliveryState.DISPATCHING)

    def test_mismatched_delivery_ids_raise_rather_than_correlate(self) -> None:
        """Attributing one delivery's evidence to another is the failure the
        whole receipt model exists to prevent. It must be loud, not silent."""
        other = _delivery("OTHER")
        result = self._deliver(other, [EVENT_INPUT_SENT, EVENT_PROMPT_SUBMITTED])

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
            self._deliver(item, [EVENT_INPUT_SENT, EVENT_PROMPT_SUBMITTED]),
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
        """The receipt carries the transport's own audit label, and no proof.

        The first version of this docstring claimed an unregistered proof method
        would be rejected. It would not: registration is checked against a
        session binding only for the session-proof evidence classes
        (``receipts.py:204-208``), and ``transport.submit_ack`` is not one of
        them — there the reducer only requires the field to be non-empty
        (``receipts.py:49``). Copilot caught the overclaim.

        What this actually asserts is narrower and true: the label is stable, and
        no ``proof`` bundle is attached, because a submit ack is transport-level
        evidence and attaching a session proof would overstate what was observed.
        """
        item = _delivery("3")
        receipt = submit_ack_receipt(
            self._deliver(item, [EVENT_INPUT_SENT, EVENT_PROMPT_SUBMITTED]),
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
