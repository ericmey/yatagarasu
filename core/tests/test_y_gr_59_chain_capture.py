"""Acceptance criterion for the dedup-to-first normalizer (#46, #59).

The chain contract is 4 events: surface.input_sent, workspace.prompt.submitted,
agent.hook.UserPromptSubmit, agent.hook.Stop. Live cmux emits 8 events for
one turn (1 input_sent, 3 prompt.submitted all carrying the SAME marker,
3 UserPromptSubmit, 1 Stop). The normalizer collapses 8 -> 4.

This test takes the literal 8-event capture from
``core/tests/ygr_round1_e2e_20260719_capture.jsonl`` and asserts
the production normalizer + validate_session_proof round-trip is wired
correctly:

  - happy path: deduped chain -> validate_session_proof returns None
  - red-proof (a): tampered marker_signature on the prompt event ->
    validate_session_proof returns "prompt_marker_binding_mismatch"
  - red-proof (b): dropping the Stop event breaks the chain shape ->
    validate_session_proof returns "source_event_chain_wrong_shape"
    (or equivalent rejection code for missing Stop)

Both red-proofs use the literal artifact: the tampered marker_signature
is the validator's own marker.signature replaced with a forged value;
the dropped Stop is the literal capture's last event filtered out.

Acceptance shape (per the seam framework):
  - Cross-module: the test exercises BOTH core validation AND the
    normalizer, with the literal 8-event capture as input.
  - Red-proofed: each rejection case is named against the actual
    rejection code from ``core/yatagarasu_core/proofs.py:226-277``.
  - End-to-end: the test runs the production path; no hand-built
    4-event chain, no reconstruction.

The dedup helper in ``_ygr_round1_e2e_capture.py`` mirrors Yua's
production normalizer for now. Once the production normalizer ships
in #46/#59, the test should call the production path directly rather
than the local helper.
"""

from __future__ import annotations

from _ygr_round1_e2e_capture import (
    build_deduped_proof,
    load_raw_events,
    make_registration,
)

from yatagarasu_core import EvidenceClass, MarkerAuthority
from yatagarasu_core.proofs import validate_session_proof

KEY = b"ygr-round1-e2e-fixture-key"


def test_fixture_loads_eight_events_verbatim() -> None:
    """Sanity check on the fixture itself.

    The literal capture has exactly 8 events. seq numbers are
    strictly increasing. Real null session_ids on
    workspace.prompt.submitted. Real null surface_ids on the hook
    events. A hand-built chain would have to choose these values
    explicitly; the fixture preserves them from the live capture.
    """
    events = load_raw_events()
    assert len(events) == 8, (
        "the literal capture has exactly 8 events; if this asserts "
        "the JSONL was edited, the fixture is no longer load-bearing"
    )

    seqs = [e["seq"] for e in events]
    assert seqs == sorted(seqs), "seq numbers must be strictly increasing"
    from itertools import pairwise

    assert all(later > earlier for earlier, later in pairwise(seqs)), (
        "seq numbers must be strictly increasing (no duplicates)"
    )

    # prompt.submitted events must carry a marker_present:true
    # (the bounded preview) and have null session_id and null surface_id
    for event in events:
        if event["name"] == "workspace.prompt.submitted":
            assert event["marker_present"] is True
            assert event["session_id"] is None
            assert event["surface_id"] is None

    # hook events must NOT carry a marker and must carry session_id
    for event in events:
        if event["name"].startswith("agent.hook."):
            assert event["marker_present"] is False
            assert event["session_id"] is not None


def test_deduped_chain_validates_against_session_proof() -> None:
    """The acceptance criterion: literal 8-event capture -> deduped
    4-event chain -> validate_session_proof returns None.

    This is the round-trip Yua's normalizer must produce. The chain
    is taken from the literal capture (input_sent + first
    prompt.submitted + first UserPromptSubmit + Stop), the marker
    is minted by MarkerAuthority with the SAME delivery as the
    capture's workspace context, and validate_session_proof is the
    production validator.
    """
    authority = MarkerAuthority(KEY)
    proof, delivery = build_deduped_proof(authority)

    rejection = validate_session_proof(
        proof=proof,
        delivery=delivery,
        evidence_class=EvidenceClass.HARNESS_TURN_COMPLETED,
        registration=make_registration(),
        marker_authority=authority,
        observed_at="2026-07-19T16:20:00Z",
    )

    assert rejection is None, (
        f"validate_session_proof returned {rejection!r} for the deduped "
        "literal capture; expected None (acceptance). The normalizer "
        "must produce a chain that the validator accepts, by VALUE."
    )


def test_tampered_marker_signature_returns_prompt_marker_binding_mismatch() -> None:
    """Red-proof (a) by name.

    The chain shape is correct, the chain order is correct, the seq
    numbers are strictly increasing. Only the prompt event's
    marker_signature is forged. The validator must reject by name
    with ``prompt_marker_binding_mismatch`` — the rejection code
    from ``core/yatagarasu_core/proofs.py:268-273``.

    A value-tampering case that would pass a shape-only check. The
    property Aoi named: "make a regex tweak obviously wrong." If the
    validator only checked "is marker_signature a non-empty string?"
    the forgery would pass. The red-proof asserts the value-level
    rejection.
    """
    authority = MarkerAuthority(KEY)
    proof, delivery = build_deduped_proof(
        authority, tamper_marker_signature="forged-signature-attacker"
    )

    rejection = validate_session_proof(
        proof=proof,
        delivery=delivery,
        evidence_class=EvidenceClass.HARNESS_TURN_COMPLETED,
        registration=make_registration(),
        marker_authority=authority,
        observed_at="2026-07-19T16:20:00Z",
    )

    assert rejection == "prompt_marker_binding_mismatch", (
        f"a forged marker_signature must produce "
        f"'prompt_marker_binding_mismatch' by name; got {rejection!r}. "
        "A value-tampering case that passes a shape-only check is a "
        "guard that looks tighter and is simply absent."
    )


def test_dropping_stop_event_breaks_chain_shape() -> None:
    """Red-proof (b).

    The deduped chain minus the Stop event is 3 events, not 4.
    validate_session_proof expects _COMPLETED_CHAIN (4 events).
    A missing Stop means the chain shape is wrong. The validator
    must reject with a name that names the missing tail.

    From proofs.py:240-243, the chain shape check is
    ``source_event_chain_wrong_shape`` when the tuple of event
    names doesn't match expected. The proof carries 3 events
    (no Stop), so the rejection must be this code.
    """
    authority = MarkerAuthority(KEY)
    proof, delivery = build_deduped_proof(authority, drop_stop_event=True)

    rejection = validate_session_proof(
        proof=proof,
        delivery=delivery,
        evidence_class=EvidenceClass.HARNESS_TURN_COMPLETED,
        registration=make_registration(),
        marker_authority=authority,
        observed_at="2026-07-19T16:20:00Z",
    )

    assert rejection == "source_event_chain_wrong_shape", (
        f"a 3-event chain (missing Stop) must produce "
        f"'source_event_chain_wrong_shape'; got {rejection!r}. "
        "The validator's chain-shape check is the proof that "
        "_COMPLETED_CHAIN is required and not just one shape."
    )


def test_swapping_event_order_breaks_source_event_chain_out_of_order() -> None:
    """Red-proof (c).

    Build a chain with the correct event-name order
    (input_sent, prompt.submitted, UserPromptSubmit, Stop) but
    with seq numbers that are NOT strictly increasing. The
    validator's strict-monotonicity check (proofs.py:265)
    returns ``source_event_chain_out_of_order`` because
    ``left.seq >= right.seq`` for at least one adjacent pair.

    A normalizer that loses seq monotonicity (drops seq, reorders
    events, or assigns the same seq to two events) produces this
    rejection. The red-proof asserts the rejection by name.
    """

    from _ygr_round1_e2e_capture import (
        BINDING_ID as _BINDING_ID,
    )
    from _ygr_round1_e2e_capture import (
        BOOT_ID as _BOOT_ID,
    )
    from _ygr_round1_e2e_capture import (
        SESSION_ID as _SESSION_ID,
    )
    from _ygr_round1_e2e_capture import (
        SOURCE_INSTANCE as _SOURCE_INSTANCE,
    )
    from _ygr_round1_e2e_capture import (
        make_marker as _make_marker,
    )

    from yatagarasu_core import SessionProof, SourceEventRef

    authority = MarkerAuthority(KEY)
    marker = _make_marker(authority)

    # Correct event-name order, but seq numbers are decreasing.
    decreasing_seqs = [25608, 25596, 25588, 25573]
    name_order = [
        "surface.input_sent",
        "workspace.prompt.submitted",
        "agent.hook.UserPromptSubmit",
        "agent.hook.Stop",
    ]

    source_events = tuple(
        SourceEventRef(
            source_instance_id=_SOURCE_INSTANCE,
            boot_id=_BOOT_ID,
            seq=decreasing_seqs[i],
            source_event_id=f"out-of-order-{i}",
            event_name=name_order[i],
            session_id=_SESSION_ID if name_order[i].startswith("agent.hook.") else None,
            binding_id=_BINDING_ID
            if name_order[i] == "workspace.prompt.submitted"
            else None,
            marker_signature=marker.signature
            if name_order[i] == "workspace.prompt.submitted"
            else None,
        )
        for i in range(4)
    )
    proof = SessionProof(
        session_id=_SESSION_ID,
        marker=marker,
        source_events=source_events,
        turn_id="turn-ygr-round1-e2e",
    )
    from _ygr_round1_e2e_capture import (
        ATTEMPT_ID,
        DELIVERY_ID,
        EVENT_ID,
    )

    from yatagarasu_core import Delivery, DeliveryMode

    delivery = Delivery(
        event_id=EVENT_ID,
        delivery_id=DELIVERY_ID,
        attempt_id=ATTEMPT_ID,
        binding_id=_BINDING_ID,
        recipient_id="codex-recipient",
        delivery_mode=DeliveryMode.SESSION_BOUND,
    )

    rejection = validate_session_proof(
        proof=proof,
        delivery=delivery,
        evidence_class=EvidenceClass.HARNESS_TURN_COMPLETED,
        registration=make_registration(),
        marker_authority=authority,
        observed_at="2026-07-19T16:20:00Z",
    )

    assert rejection == "source_event_chain_out_of_order", (
        f"a chain with correct event-name order but non-increasing "
        f"seq numbers must produce 'source_event_chain_out_of_order'; "
        f"got {rejection!r}. A normalizer that loses seq monotonicity "
        "is broken."
    )
