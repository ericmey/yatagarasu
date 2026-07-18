import pytest
from yatagarasu_cmux.journal import InjectionJournal, JournalState
from yatagarasu_cmux.marker import extract, mint


def test_hook_011_restart_every_tracked_delivery_accountable(tmp_path):
    """
    Y-CMUX-011 — Restart: every tracked delivery is accountable, with ambiguity visible.
    """
    path = tmp_path / "j.sqlite"
    j = InjectionJournal(path)

    # Fixture: submit 5 sends
    j.prepare(
        delivery_id="d-1", binding_id="b", seat_id="s", marker="[ygr:d-1:n:s]", now=1.0
    )
    j.prepare(
        delivery_id="d-2", binding_id="b", seat_id="s", marker="[ygr:d-2:n:s]", now=1.0
    )
    j.prepare(
        delivery_id="d-3", binding_id="b", seat_id="s", marker="[ygr:d-3:n:s]", now=1.0
    )
    j.prepare(
        delivery_id="d-4", binding_id="b", seat_id="s", marker="[ygr:d-4:n:s]", now=1.0
    )
    j.prepare(
        delivery_id="d-5", binding_id="b", seat_id="s", marker="[ygr:d-5:n:s]", now=1.0
    )

    # Progress states before crash
    j.settle(delivery_id="d-2", state=JournalState.INJECTED, now=2.0)

    # d-3: Must transition to INJECTED before ACKED (State machine enforcement from PR #16)
    j.settle(delivery_id="d-3", state=JournalState.INJECTED, now=2.0)
    j.settle(delivery_id="d-3", state=JournalState.ACKED, now=2.0)

    j.settle(delivery_id="d-4", state=JournalState.AMBIGUOUS, now=2.0)
    # d-1 and d-5 remain prepared (in crash window)

    # Crash!
    j.close()

    # Post-restart recovery
    j2 = InjectionJournal(path)
    try:
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

    finally:
        j2.close()


@pytest.mark.skip(
    reason="Y-CMUX-012 requires the receipt emitter implementation; see issue #28"
)
def test_hook_012_turn_completed_never_answered():
    """
    Y-CMUX-012 — harness.turn_completed -> only processed(completed)
    A bare turn-end does not prove answered / acknowledged / held / declined.
    """
    pass


def test_hook_013_signed_marker_forgery_rejected():
    """
    Y-CMUX-013: Signed marker validation; forged/expired/copied/stale rejected.
    """
    real_key = b"strict-signing-key"
    empty_key = b""
    delivery_id = "d-123"

    # Mint a real marker
    marker = mint(real_key, delivery_id)
    assert marker is not None
    assert extract(real_key, marker.text) is not None

    # Fixture A - Forged Marker: tampered signature
    forged_text = f"[ygr:{delivery_id}:{marker.nonce}:{'0' * 16}]"
    assert extract(real_key, forged_text) is None, "Forged marker must be rejected"

    # Fixture C/D - Copied/Stale Marker
    tampered_id = f"[ygr:wrong-id:{marker.nonce}:{marker.signature}]"
    assert extract(real_key, tampered_id) is None, (
        "Tampered delivery ID must fail signature"
    )

    # CRITICAL SECURITY REGRESSION: The empty key vulnerability
    assert extract(empty_key, marker.text) is None, (
        "Empty key must reject real markers (fail closed)"
    )
    assert extract(empty_key, forged_text) is None, (
        "Empty key must reject forged markers"
    )


@pytest.mark.skip(
    reason="Y-CMUX-014 requires the network queue simulator to assert bounded retries without reinjection; see issue #29"
)
def test_hook_014_receipt_endpoint_outage():
    """
    Y-CMUX-014 — Receipt endpoint outage: transport-submitted holds; retry proof, NEVER reinject
    The evidence provider is observer-only. It durably queues the receipt locally for bounded retry.
    """
    pass
