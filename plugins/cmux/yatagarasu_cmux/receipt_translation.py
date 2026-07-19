"""The seam out of the injector: ``SubmitResult`` -> core ``Receipt``.

Addresses #50, found in Tama's seam audit (#48). Every production send through the
cmux plugin ends at a ``SubmitResult`` that goes nowhere — the core reducer has no
record that the send ever happened. Both halves were unit-green; nothing crossed
between them.

**Not yet wired into production.** Present tense above is deliberate: this module
supplies the missing translation and proves it against the real reducer, but
nothing in production calls ``submit_ack_receipt`` yet. Copilot caught the first
version of this docstring claiming the dead-end was already fixed — which would
have been the same defect the seam audit exists to find, in the fix for it.

The caller lands with the injector's ``MarkerAuthority`` change (#47), because
that is where ``Injector.deliver`` acquires the ``Delivery`` record this function
needs as its second argument. Until then #50 stays open.

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

#: What the cmux transport claims about how it knows.
#:
#: Deliberately understated, because the first draft of this comment said the
#: reducer "registers proof methods, so this string is a contract, not a label"
#: and Copilot caught that it overstates the enforcement. Proof-method
#: registration is checked against a session binding only for the session-proof
#: evidence classes (``receipts.py:204-208``, inside ``_validate_session_receipt``).
#: For ``transport.submit_ack`` the reducer only requires the field to be
#: non-empty (``receipts.py:49``).
#:
#: So this is an audit label the reducer stores and compares for receipt
#: identity, not a credential it validates. Saying otherwise would describe a
#: check that does not run.
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
    module invents, and it is worth being exact about why — the first version of
    this paragraph was not.

    It claimed a hard-coded timestamp would be used to enforce binding lifetimes
    and marker validity windows. That enforcement is real but it lives in
    ``_validate_session_receipt`` (``receipts.py:194,215``) and runs only for the
    session-proof evidence classes. ``transport.submit_ack`` is not one of them.
    Tama caught the overclaim.

    What ``observed_at`` actually does here: the reducer requires it non-empty
    (``receipts.py:49``) and stores it, and it is one of the fields compared when
    deciding whether a resubmitted receipt is the same receipt
    (``receipts.py:159``). So a constant does not get the receipt rejected — it
    quietly corrupts the audit record, making every send claim to have been
    observed at the same instant and rendering genuinely distinct observations
    indistinguishable. A weaker consequence than the one first claimed, and still
    a sufficient reason for the caller to own the clock.

    A derived ``receipt_id`` would silently collide across attempts.

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
