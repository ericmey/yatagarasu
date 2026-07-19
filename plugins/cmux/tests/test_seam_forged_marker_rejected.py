"""The regression test for the forgery hole closed on PR #42.

Written by Aoi as merge authority, because the fix was correct and nothing in the
suite proved it. Reverting the two lines that source the correlation fields from
the wire would have left every existing test green.

**The hole.** The emitter populated ``prompt.binding_id`` and
``prompt.marker_signature`` from the *authoritative* delivery lookup, and set
``proof.marker`` to that same authoritative marker. Core's guard
(``proofs.py:268-273``) then compared a value against itself:

    prompt.marker_signature != proof.marker.signature
    -> core_marker.signature != core_marker.signature   # never true

The guard still ran, still read like verification, and could no longer fail. And
because the only field taken from the observed prompt was ``delivery_id``, anyone
who could get text into a prompt could paste a token carrying a real
``delivery_id`` and mint a valid ``harness.turn_completed`` receipt.

**Why the existing assertion did not catch it.** ``test_hook_012`` asserts the
prompt event's ``binding_id == "b-2"`` and calls that proof of independent
origins. In that fixture the wire marker and the authoritative delivery *both*
carry ``b-2``, so the assertion passes whichever end the value came from. Two
origins that happen to agree cannot demonstrate that they are two origins.

So this test makes them disagree, which is the only way to tell. A marker is
minted with an attacker's key for a real ``delivery_id``. ``MarkerAuthority.decode``
accepts it — decoding is deliberately not authorization — so the emitter stamps
the prompt with the *forged* signature while the proof carries the *authoritative*
one. They differ, and the rejection code says so by name.

Under the pre-fix emitter this test fails by returning ``None`` (accepted), which
is the hole reported as a passing forgery.
"""

from __future__ import annotations

import pytest
from yatagarasu_cmux.receipt_emitter import ReceiptEmitter

from yatagarasu_core import Delivery, DeliveryMode, EvidenceClass, SourceEventRef
from yatagarasu_core.proofs import (
    CorrelationRule,
    MarkerAuthority,
    ProofMethodRegistration,
    SourceKind,
    validate_session_proof,
)

REAL_KEY = b"the-authoritative-signing-key"
ATTACKER_KEY = b"a-key-the-authority-never-issued"
SOURCE_INSTANCE = "src"
PROOF_METHOD = "cmux.event_bus.harness_hook_relay"
ISSUED_AT = "2026-07-19T21:00:00Z"
EXPIRES_AT = "2026-07-19T21:05:00Z"
OBSERVED_AT = "2026-07-19T21:01:00Z"


def _registration() -> ProofMethodRegistration:
    return ProofMethodRegistration(
        proof_method=PROOF_METHOD,
        source_kind=SourceKind.EVENT_BUS,
        source_instance_id=SOURCE_INSTANCE,
        correlation_rule=CorrelationRule.CMUX_HARNESS_CHAIN,
        evidence_classes=frozenset({EvidenceClass.HARNESS_TURN_COMPLETED}),
    )


def _run_chain(marker_token_in_prompt: str, delivery: Delivery, authoritative):
    """Drive the emitter through one full chain and return the emitted receipt."""
    emitted: list = []
    emitter = ReceiptEmitter(
        core_client=emitted.append,
        provider_id="cmux-provider",
        delivery_lookup=lambda did: (
            (delivery, authoritative) if did == delivery.delivery_id else None
        ),
    )

    emitter.observe(
        SourceEventRef(SOURCE_INSTANCE, "boot", 1, "e1", "surface.input_sent"),
        observed_at=OBSERVED_AT,
    )
    emitter.observe(
        SourceEventRef(SOURCE_INSTANCE, "boot", 2, "e2", "workspace.prompt.submitted"),
        payload={"message_preview": marker_token_in_prompt},
        observed_at=OBSERVED_AT,
    )
    emitter.observe(
        SourceEventRef(
            SOURCE_INSTANCE,
            "boot",
            3,
            "e3",
            "agent.hook.UserPromptSubmit",
            session_id="s-1",
        ),
        observed_at=OBSERVED_AT,
    )
    emitter.observe(
        SourceEventRef(
            SOURCE_INSTANCE, "boot", 4, "e4", "agent.hook.Stop", session_id="s-1"
        ),
        observed_at=OBSERVED_AT,
    )
    return emitted


@pytest.fixture
def delivery() -> Delivery:
    return Delivery("ev-1", "d-1", "a-1", "b-1", "yua", DeliveryMode.SESSION_BOUND)


def test_a_forged_marker_in_the_prompt_is_rejected_by_core(delivery) -> None:
    """The hole, stated as a test. A marker signed with a key the authority never
    issued still decodes, so the emitter will happily build a chain from it. Core
    must refuse it, and must refuse it for the *binding* reason."""
    authority = MarkerAuthority(REAL_KEY)
    attacker = MarkerAuthority(ATTACKER_KEY)

    authoritative = authority.mint(delivery, issued_at=ISSUED_AT, expires_at=EXPIRES_AT)
    forged = attacker.mint(delivery, issued_at=ISSUED_AT, expires_at=EXPIRES_AT)
    assert forged.signature != authoritative.signature, "fixture is not adversarial"

    emitted = _run_chain(attacker.encode(forged), delivery, authoritative)
    assert len(emitted) == 1, "the emitter builds a chain; core is what refuses it"

    error = validate_session_proof(
        proof=emitted[0].proof,
        delivery=delivery,
        evidence_class=EvidenceClass.HARNESS_TURN_COMPLETED,
        registration=_registration(),
        marker_authority=authority,
        observed_at=OBSERVED_AT,
    )

    assert error == "prompt_marker_binding_mismatch", (
        "a forged marker produced an ACCEPTED receipt — the prompt's correlation"
        " fields are being sourced from the authoritative record instead of the"
        " observed wire, so core is comparing a value against itself"
    )


def test_the_authentic_marker_still_validates(delivery) -> None:
    """The other half. A guard that rejects everything is as useless as one that
    rejects nothing, so the honest chain must pass the same validator."""
    authority = MarkerAuthority(REAL_KEY)
    authoritative = authority.mint(delivery, issued_at=ISSUED_AT, expires_at=EXPIRES_AT)

    emitted = _run_chain(authority.encode(authoritative), delivery, authoritative)
    assert len(emitted) == 1

    error = validate_session_proof(
        proof=emitted[0].proof,
        delivery=delivery,
        evidence_class=EvidenceClass.HARNESS_TURN_COMPLETED,
        registration=_registration(),
        marker_authority=authority,
        observed_at=OBSERVED_AT,
    )

    assert error is None, f"the honest path must validate, got {error!r}"
