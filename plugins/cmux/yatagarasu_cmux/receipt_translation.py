"""The seam out of the injector: ``SubmitResult`` -> core ``Receipt``.

Closes #50, found in Tama's seam audit (#48). Every production send through the
cmux plugin ended at a ``SubmitResult`` that went nowhere — the core reducer had
no record that the send ever happened. Both halves were unit-green; nothing
crossed between them.

The whole of the design is in what this module *refuses* to translate.

``SubmitResult`` carries a three-outcome contract, and the two negatives are not
symmetric:

``SUBMITTED``
    The transport observed its own acknowledgement. This is real evidence and it
    becomes a ``transport.submit_ack`` receipt.
``NOT_SUBMITTED``
    A clean, proven negative — the send demonstrably did not land, so it is safe
    to requeue. It produces **no** receipt: the delivery simply never left
    ``dispatching``, which is the truth.
``UNKNOWN``
    We do not know. The send may have landed. It produces **no** receipt, and
    that is the point of the whole outcome type — emitting one here would assert
    a transport acknowledgement we never observed, and the reducer would advance
    the delivery out of ``dispatching`` on the strength of a guess.

So the honest return type is ``Receipt | None``, and ``None`` is a normal answer
rather than a failure. A translator that always produced a receipt would make the
outcome enum decorative.
"""

from __future__ import annotations

from yatagarasu_core import Delivery, EvidenceClass, Receipt

from .outcome import SubmitOutcome, SubmitResult

#: What the cmux transport claims about how it knows. The reducer registers
#: proof methods, so this string is a contract, not a label.
PROOF_METHOD = "cmux.transport.submit_ack"


def submit_ack_receipt(
    result: SubmitResult,
    delivery: Delivery,
    *,
    evidence_provider_id: str,
    observed_at: str,
    receipt_id: str,
    source_event_id: str | None = None,
) -> Receipt | None:
    """Translate a proven submit into a receipt, or return ``None``.

    ``observed_at`` and ``receipt_id`` are parameters rather than values this
    module invents. A hard-coded timestamp would be read by the reducer as a real
    observation and used to enforce binding lifetimes and marker validity windows
    against a time that never happened; a derived receipt id would silently
    collide across attempts. Both belong to the caller that owns the clock.

    ``delivery`` is required because a ``SubmitResult`` knows only its
    ``delivery_id``. Every other correlation field the reducer needs —
    ``event_id``, ``attempt_id``, ``binding_id`` — lives on the delivery record.
    Taking them from the delivery rather than re-deriving them is what makes this
    a translation instead of a second, divergent source of truth.

    No ``proof`` bundle is attached, deliberately. ``transport.submit_ack`` is
    transport-level evidence: the transport saw its own ack. It is not a claim
    about what the agent did with the prompt, and the reducer does not ask for a
    session proof here. Attaching one would overstate what was observed.
    """
    if result.outcome is not SubmitOutcome.SUBMITTED:
        return None

    if result.delivery_id != delivery.delivery_id:
        raise ValueError(
            f"submit result is for delivery {result.delivery_id!r} but was given"
            f" delivery {delivery.delivery_id!r}; correlating these would attribute"
            " one delivery's evidence to another"
        )

    if delivery.binding_id is None:
        raise ValueError(
            f"delivery {delivery.delivery_id!r} has no binding; a receipt without"
            " a binding cannot be correlated to a session and the reducer would"
            " reject it on shape rather than on substance"
        )

    return Receipt(
        receipt_id=receipt_id,
        event_id=delivery.event_id,
        delivery_id=delivery.delivery_id,
        attempt_id=delivery.attempt_id,
        binding_id=delivery.binding_id,
        evidence_provider_id=evidence_provider_id,
        evidence_class=EvidenceClass.TRANSPORT_SUBMIT_ACK,
        proof_method=PROOF_METHOD,
        observed_at=observed_at,
        source_event_id=source_event_id,
        # The reducer rejects transport.submit_ack carrying a disposition:
        # acknowledging a submit says nothing about how the turn ended.
        disposition=None,
    )
