"""Tests for the durable injection journal.

The journal exists for exactly one scenario: the process dies between injecting
into a pane and recording that it did. Every test here is about that window or
its consequences.
"""

from __future__ import annotations

import pytest
from yatagarasu_cmux.journal import (
    InjectionJournal,
    JournalError,
    JournalState,
)


@pytest.fixture
def journal(tmp_path):
    with InjectionJournal(tmp_path / "j.sqlite") as j:
        yield j


def prep(j, delivery_id="d-1", seat="seat-a", now=1.0):
    return j.prepare(
        delivery_id=delivery_id,
        binding_id="b-1",
        seat_id=seat,
        marker=f"[ygr:{delivery_id}:aa:bb]",
        now=now,
    )


def test_prepare_records_intent_as_prepared(journal):
    row = prep(journal)
    assert row.state is JournalState.PREPARED
    assert row.needs_reconciliation
    assert not row.is_terminal


def test_prepare_refuses_to_overwrite_an_existing_row(journal):
    """A second prepare for the same delivery would erase evidence that an effect
    may already have fired — which is the one thing this module must never do."""
    prep(journal)
    with pytest.raises(JournalError, match="already journaled"):
        prep(journal)


def test_broadcast_fan_out_is_not_suppressed(journal):
    """The load-bearing key decision. One event fans out to many deliveries; if
    the journal were keyed on event_id, the second recipient would look like a
    duplicate and be dropped."""
    prep(journal, "d-seat-a", seat="seat-a")
    prep(journal, "d-seat-b", seat="seat-b")
    prep(journal, "d-seat-c", seat="seat-c")

    assert len(journal.unsettled()) == 3
    assert {r.seat_id for r in journal.unsettled()} == {"seat-a", "seat-b", "seat-c"}


def test_settle_to_injected_then_acked(journal):
    prep(journal)
    journal.settle(
        delivery_id="d-1",
        state=JournalState.INJECTED,
        now=2.0,
        source_events=("surface.input_sent", "workspace.prompt.submitted"),
    )
    row = journal.settle(delivery_id="d-1", state=JournalState.ACKED, now=3.0)

    assert row.state is JournalState.ACKED
    assert row.is_terminal
    assert row.source_events == ("surface.input_sent", "workspace.prompt.submitted")


def test_crash_window_row_survives_and_is_visible(tmp_path):
    """Simulates the crash: prepare, then drop the process without settling.
    On reopen the row must still be there, and must read as AMBIGUOUS-pending
    rather than as absent."""
    path = tmp_path / "j.sqlite"
    j1 = InjectionJournal(path)
    prep(j1)
    j1.close()  # no settle — as if killed mid-injection

    j2 = InjectionJournal(path)
    try:
        stuck = j2.unsettled()
        assert len(stuck) == 1
        assert stuck[0].delivery_id == "d-1"
        assert stuck[0].needs_reconciliation
    finally:
        j2.close()


def test_ambiguous_is_terminal_and_not_rewritable(journal):
    """A held ambiguity must not be quietly promoted to success later."""
    prep(journal)
    journal.settle(
        delivery_id="d-1",
        state=JournalState.AMBIGUOUS,
        now=2.0,
        detail="marker unprovable on the bus",
    )
    with pytest.raises(JournalError, match="already terminal"):
        journal.settle(delivery_id="d-1", state=JournalState.ACKED, now=3.0)


def test_settle_cannot_move_backwards_to_prepared(journal):
    prep(journal)
    with pytest.raises(JournalError, match="cannot move a row back"):
        journal.settle(delivery_id="d-1", state=JournalState.PREPARED, now=2.0)


def test_settling_an_unprepared_delivery_is_refused(journal):
    """Settling without a prepare means the intent record was skipped — the exact
    ordering violation the journal exists to prevent."""
    with pytest.raises(JournalError, match="never prepared"):
        journal.settle(delivery_id="ghost", state=JournalState.INJECTED, now=1.0)


def test_terminal_rows_are_not_in_the_reconciliation_set(journal):
    prep(journal, "d-done")
    prep(journal, "d-stuck")
    journal.settle(delivery_id="d-done", state=JournalState.ACKED, now=2.0)

    assert [r.delivery_id for r in journal.unsettled()] == ["d-stuck"]


def test_journal_stores_no_message_content(journal):
    """Only the marker identifies an attempt. Bodies never reach durable state."""
    row = prep(journal)
    assert "ygr" in row.marker
    for value in (row.marker, row.detail, row.binding_id, row.seat_id):
        assert "hello" not in value


def test_idempotent_settle_to_same_terminal_state_is_allowed(journal):
    """A retried ack must not explode — it is the same claim, not a new one."""
    prep(journal)
    journal.settle(delivery_id="d-1", state=JournalState.ACKED, now=2.0)
    row = journal.settle(delivery_id="d-1", state=JournalState.ACKED, now=3.0)
    assert row.state is JournalState.ACKED


def test_acking_does_not_erase_the_evidence_chain(journal):
    """Regression: settling to ACKED without re-supplying source events must not
    wipe the chain recorded at INJECTED. An ack that destroys the proof of the
    injection is the same evidence-loss failure this module exists to prevent,
    pointed at ourselves."""
    prep(journal)
    chain = ("surface.input_sent", "workspace.prompt.submitted")
    journal.settle(
        delivery_id="d-1", state=JournalState.INJECTED, now=2.0, source_events=chain
    )
    acked = journal.settle(delivery_id="d-1", state=JournalState.ACKED, now=3.0)

    assert acked.source_events == chain


def test_later_settle_may_add_evidence_but_not_silently_drop_it(journal):
    prep(journal)
    journal.settle(
        delivery_id="d-1",
        state=JournalState.INJECTED,
        now=2.0,
        source_events=("surface.input_sent",),
        detail="first",
    )
    row = journal.settle(
        delivery_id="d-1",
        state=JournalState.ACKED,
        now=3.0,
        source_events=("surface.input_sent", "workspace.prompt.submitted"),
    )
    assert row.source_events == ("surface.input_sent", "workspace.prompt.submitted")
    assert row.detail == "first"
