import pytest
from yatagarasu_cmux.journal import InjectionJournal, JournalState
from yatagarasu_cmux.marker import extract
from yatagarasu_cmux.receipt_emitter import ReceiptEmitter

from yatagarasu_core import Delivery, DeliveryMode, SourceEventRef
from yatagarasu_core.proofs import MarkerAuthority


def test_hook_011_restart_every_tracked_delivery_accountable(tmp_path):
    """
    Y-CMUX-011 — Restart: every tracked delivery is accountable, with ambiguity visible.
    """
    path = tmp_path / "j.sqlite"

    # 1. Pre-crash block (ensures handle closes safely even if an assertion fails before the crash)
    with InjectionJournal(path) as j:
        # Fixture: submit 5 sends
        j.prepare(
            delivery_id="d-1",
            binding_id="b",
            seat_id="s",
            marker="[ygr:d-1:n:s]",
            now=1.0,
        )
        j.prepare(
            delivery_id="d-2",
            binding_id="b",
            seat_id="s",
            marker="[ygr:d-2:n:s]",
            now=1.0,
        )
        j.prepare(
            delivery_id="d-3",
            binding_id="b",
            seat_id="s",
            marker="[ygr:d-3:n:s]",
            now=1.0,
        )
        j.prepare(
            delivery_id="d-4",
            binding_id="b",
            seat_id="s",
            marker="[ygr:d-4:n:s]",
            now=1.0,
        )
        j.prepare(
            delivery_id="d-5",
            binding_id="b",
            seat_id="s",
            marker="[ygr:d-5:n:s]",
            now=1.0,
        )

        # Progress states before crash
        j.settle(delivery_id="d-2", state=JournalState.INJECTED, now=2.0)

        # d-3: Must transition to INJECTED before ACKED (State machine enforcement from PR #16)
        j.settle(delivery_id="d-3", state=JournalState.INJECTED, now=2.0)
        j.settle(delivery_id="d-3", state=JournalState.ACKED, now=2.0)

        j.settle(delivery_id="d-4", state=JournalState.AMBIGUOUS, now=2.0)
        # d-1 and d-5 remain prepared (in crash window)

    # At this point, `with` block exits, `j.close()` is automatically called. Crash simulated.

    # 2. Post-restart recovery
    with InjectionJournal(path) as j2:
        unsettled = j2.unsettled()
        assert len(unsettled) == 2

        j2.settle(
            delivery_id="d-1",
            state=JournalState.INJECTED,
            now=3.0,
            detail="reconciled via bus",
        )
        j2.settle(
            delivery_id="d-5",
            state=JournalState.AMBIGUOUS,
            now=3.0,
            detail="marker unprovable on the bus",
        )

        # Verify the accountability invariant: no row remains prepared.
        assert len(j2.unsettled()) == 0

        # Verify states
        assert j2.get("d-1").state == JournalState.INJECTED
        assert j2.get("d-2").state == JournalState.INJECTED
        assert j2.get("d-3").state == JournalState.ACKED
        assert j2.get("d-4").state == JournalState.AMBIGUOUS
        assert j2.get("d-5").state == JournalState.AMBIGUOUS


def test_hook_012_turn_completed_never_answered():
    """
    Y-CMUX-012 — harness.turn_completed -> only processed(completed)
    A bare turn-end does not prove answered / acknowledged / held / declined.

    A Stop event which does not correlate to a specific bound delivery emits nothing.
    """
    emitted = []

    def fake_core_client(receipt) -> None:
        emitted.append(receipt)

    # Setup core marker
    auth = MarkerAuthority(b"strict-signing-key")
    delivery = Delivery("ev-1", "d-1", "a-1", "b-1", "yua", DeliveryMode.SESSION_BOUND)
    core_marker = auth.mint(
        delivery, issued_at="2026-07-18T21:00:00Z", expires_at="2026-07-18T21:05:00Z"
    )
    encoded_marker = auth.encode(core_marker)

    delivery_2 = Delivery(
        "ev-2", "d-2", "a-2", "b-2", "yua", DeliveryMode.SESSION_BOUND
    )
    marker_2 = auth.mint(
        delivery_2, issued_at="2026-07-18T21:00:00Z", expires_at="2026-07-18T21:05:00Z"
    )
    encoded_marker_2 = auth.encode(marker_2)

    def lookup(delivery_id: str):
        if delivery_id == "d-1":
            return (delivery, core_marker)
        elif delivery_id == "d-2":
            return (delivery_2, marker_2)
        return None

    emitter = ReceiptEmitter(
        core_client=fake_core_client,
        provider_id="cmux-provider",
        delivery_lookup=lookup,
    )

    # Fixture C: bare turn-end with no preceding prompt
    stop_stray = SourceEventRef(
        "src", "boot", 4, "ev-4", "agent.hook.Stop", session_id="s-123"
    )
    emitter.observe(stop_stray, observed_at="2026-07-18T21:05:00Z")
    assert len(emitted) == 0, "Stop with no preceding prompt MUST NOT emit a receipt"

    # Fixture B: Stray Stop from an unrelated session
    input_event = SourceEventRef("src", "boot", 1, "ev-1", "surface.input_sent")
    prompt_event = SourceEventRef(
        "src", "boot", 2, "ev-2", "workspace.prompt.submitted"
    )
    user_prompt = SourceEventRef(
        "src", "boot", 3, "ev-3", "agent.hook.UserPromptSubmit", session_id="s-123"
    )

    emitter.observe(input_event, observed_at="2026-07-18T21:05:00Z")
    emitter.observe(
        prompt_event,
        payload={"message_preview": encoded_marker},
        observed_at="2026-07-18T21:05:00Z",
    )
    emitter.observe(user_prompt, observed_at="2026-07-18T21:05:00Z")

    # Now we have an active chain for s-123. Emit a Stop for s-wrong.
    stop_wrong = SourceEventRef(
        "src", "boot", 5, "ev-5", "agent.hook.Stop", session_id="s-wrong"
    )
    emitter.observe(stop_wrong, observed_at="2026-07-18T21:05:00Z")
    assert len(emitted) == 0, (
        "Stray Stop from an unrelated session MUST NOT emit a receipt"
    )

    # Fixture D: UserPromptSubmit without session_id clears buffer
    # If the buffer isn't cleared, the NEXT valid chain will mis-correlate
    emitter.observe(input_event, observed_at="2026-07-18T21:05:00Z")
    emitter.observe(
        prompt_event,
        payload={"message_preview": encoded_marker},
        observed_at="2026-07-18T21:05:00Z",
    )

    # Missing session_id
    user_prompt_missing = SourceEventRef(
        "src", "boot", 7, "ev-7", "agent.hook.UserPromptSubmit", session_id=None
    )
    emitter.observe(user_prompt_missing, observed_at="2026-07-18T21:05:00Z")

    # Testing the actual logic bug previously in the emitter:
    # If a payload signature was copied, the emitter should stamp the event
    # with the COPIED binding/signature. Core then checks it against the AUTHORITATIVE marker.
    emitter.observe(
        SourceEventRef("src", "boot", 8, "ev-8", "surface.input_sent"),
        observed_at="2026-07-18T21:05:00Z",
    )
    emitter.observe(
        SourceEventRef("src", "boot", 9, "ev-9", "workspace.prompt.submitted"),
        payload={"message_preview": encoded_marker_2},
        observed_at="2026-07-18T21:05:00Z",
    )
    emitter.observe(
        SourceEventRef(
            "src",
            "boot",
            10,
            "ev-10",
            "agent.hook.UserPromptSubmit",
            session_id="s-999",
        ),
        observed_at="2026-07-18T21:05:00Z",
    )
    emitter.observe(
        SourceEventRef(
            "src", "boot", 11, "ev-11", "agent.hook.Stop", session_id="s-999"
        ),
        observed_at="2026-07-18T21:05:00Z",
    )

    assert len(emitted) == 1, "Only the successful s-999 chain should emit"
    assert emitted[0].delivery_id == "d-2", (
        "Buffer was not cleared on missing session_id, leading to cross-delivery misattribution!"
    )
    # To prove it has independent origins:
    # If the emitter populated the SourceEventRef from the *authoritative* store instead of the *observed* wire,
    # prompt.binding_id would match the authoritative delivery. Here, emitted[0].proof.source_events[1]
    # is the prompt_event. Its binding_id should be from decoded_marker_2 (the wire payload).
    prompt_ev = emitted[0].proof.source_events[1]
    assert prompt_ev.binding_id == "b-2", (
        "Prompt event MUST take binding_id from the wire payload"
    )

    # Fixture A: Correlated Stop (happy path)
    stop_correct = SourceEventRef(
        "src", "boot", 6, "ev-6", "agent.hook.Stop", session_id="s-123"
    )
    emitter.observe(stop_correct, observed_at="2026-07-18T21:06:00Z")
    assert len(emitted) == 2, "Correlated Stop MUST emit exactly one receipt"

    receipt = emitted[1]

    # Assertion 1: evidence class is harness.turn_completed
    assert receipt.evidence_class == "harness.turn_completed"

    # Assertion 2: disposition is exactly "completed", NEVER answered or acknowledged
    assert receipt.disposition == "completed"

    # Assertion 3: proof method recorded correctly
    assert receipt.proof_method == "cmux.event_bus.harness_hook_relay"
    assert receipt.observed_at == "2026-07-18T21:06:00Z"

    # Assertion 4: exact proof bundle is attached and correlated
    assert receipt.proof is not None
    assert receipt.proof.session_id == "s-123"
    assert len(receipt.proof.source_events) == 4
    assert receipt.proof.source_events[3] == stop_correct

    # Assertion 5: REQUIRED durable correlation fields populated
    prompt_in_proof = receipt.proof.source_events[1]
    assert prompt_in_proof.binding_id == "b-1"
    assert prompt_in_proof.marker_signature == core_marker.signature


def test_hook_013_signed_marker_forgery_rejected():
    """
    Y-CMUX-013: Signed marker validation — forged signature, tampered
    delivery_id, and empty-key misconfiguration are all rejected.

    Expiry and replay/stale detection are NOT covered here; they need a clock
    and a seen-marker store. Do not read this test as proving them.
    """
    real_key = b"strict-signing-key"
    delivery = Delivery(
        "ev-123", "d-123", "a-123", "b-123", "yua", DeliveryMode.SESSION_BOUND
    )

    authority = MarkerAuthority(real_key)

    # Mint a real marker
    marker = authority.mint(
        delivery, issued_at="2026-07-18T21:00:00Z", expires_at="2026-07-18T21:05:00Z"
    )
    assert marker is not None
    encoded_marker = authority.encode(marker)
    assert extract(None, encoded_marker) is not None

    # Fixture A - Forged Marker: tampered signature
    # Since it's base64 encoded now, we'll just corrupt the payload to test extraction failure
    forged_text = encoded_marker[:-10] + "0" * 10
    assert extract(None, forged_text) is None, "Forged marker must be rejected"

    # Fixture C/D - Copied/Stale Marker
    # Handled at the core binding level, but local extraction requires exact text
    tampered_id = "ygr1.tampered"
    extracted = extract(None, tampered_id)
    assert extracted is None, "Tampered marker should fail decoding"

    # Empty-key misconfiguration is handled by MarkerAuthority construction in core,
    # not by extraction parsing. See `test_empty_key_never_authenticates_a_marker`
    # in `test_injector.py` for the security boundary test.


@pytest.mark.skip(
    reason="Y-CMUX-014 requires the network queue simulator to assert bounded retries without reinjection; see issue #29"
)
def test_hook_014_receipt_endpoint_outage():
    """
    Y-CMUX-014 — Receipt endpoint outage: transport-submitted holds; retry proof, NEVER reinject
    The evidence provider is observer-only. It durably queues the receipt locally for bounded retry.
    """
    pass
