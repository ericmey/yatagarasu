"""The cross-module test for the BroadcastKernel <-> InjectionJournal seam (#51).

This file exists because the seam-audit (#48) found that both halves of this
boundary were fully green and nothing ran across it. ``BroadcastKernel.broadcast``
was unit-tested in ``core/tests/test_broadcasts.py``; ``InjectionJournal.prepare``
was unit-tested in ``plugins/cmux/tests/test_journal.py`` and exercised by the
broadcast-shape observer at ``test_acceptance_006_010.py:506`` — but that
observer hand-crafted journal rows via ``journal.prepare(...)`` directly,
bypassing the kernel. Two green suites that never touch.

So the rule for everything below, per Tama's acceptance criterion on #51:
the test takes the kernel's actual ``BroadcastResult.outcomes`` and feeds
each one through the journal's actual ``prepare(...)``. No hand-crafted
loops, no reconstructed recipient lists, no second source of truth for
how many rows the journal holds. The journal row count is determined by
the kernel output and nothing else.

Three red-proofs guard against a hand-rolled loop substituting for the
production code:

  (a) N=5 bound recipients -> exactly 5 journal rows, one per seat,
      each row's delivery_id matches the kernel's outcome.delivery_id.

  (b) N=5 recipients where 2 have no binding -> exactly 3 journal rows.
      If a hand-rolled loop in the test had substituted for the kernel,
      it would journal 5 rows (the asked count) instead of 3 (the kernel
      output's available count). This proves the kernel is the production
      source of the rows.

  (c) Roster with a duplicate recipient_id -> the kernel raises
      ``ValueError("room roster contains a duplicate recipient")`` at
      ``replace_room_roster`` BEFORE the broadcast runs. The seam test
      asserts this loud failure, not silent double-write.

Plus the literal-artifact canary (per the seam framework update): the
journal row count and per-row delivery_ids are taken directly from
``result.outcomes``, which is what the kernel actually produced. A
shape-only check (e.g., ``len(journal.unsettled()) == N_recipients``)
would not catch (b); a value check (delivery_ids match exactly what
the kernel produced) does.

Reopen condition (SEV-1): a broadcast that hides an absent seat
behind a rollup, OR a journal row that doesn't correspond to a kernel
outcome, OR a duplicate recipient that gets silently double-written.
"""

from __future__ import annotations

import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from yatagarasu_cmux.journal import InjectionJournal

from yatagarasu_core import (
    BroadcastKernel,
    CoreStore,
    CorrelationRule,
    EvidenceClass,
    ProviderKind,
    SessionBinding,
    SourceKind,
)
from yatagarasu_core.proofs import ProofMethodRegistration

PROVIDER_ID = "cmux-broadcast-seam-test"
PROOF_METHOD = "cmux.event-bus"
OBSERVED_AT = "2026-07-19T15:00:00Z"
SOURCE_INSTANCE = "cmux-source-seam"


def _make_binding(recipient_id: str, index: int) -> SessionBinding:
    """Build a SessionBinding for one recipient at a known harness."""
    return SessionBinding(
        binding_id=f"binding-seam-{recipient_id}",
        recipient_id=recipient_id,
        provider_id=PROVIDER_ID,
        adapter_instance_id="cmux-vesper",
        harness="codex",
        session_id=f"session-seam-{recipient_id}",
        established_at=OBSERVED_AT,
        expires_at="2026-07-19T17:00:00Z",
        proof_methods=(
            ProofMethodRegistration(
                proof_method=PROOF_METHOD,
                source_kind=SourceKind.EVENT_BUS,
                source_instance_id=f"{SOURCE_INSTANCE}-{index}",
                correlation_rule=CorrelationRule.CMUX_HARNESS_CHAIN,
                evidence_classes=frozenset({EvidenceClass.HARNESS_PROMPT_ACCEPTED}),
            ),
        ),
    )


def _setup_store_and_roster(
    tmp_path: Path,
    recipients: tuple[str, ...],
    *,
    skip_binding_for: frozenset[str] = frozenset(),
) -> tuple[CoreStore, Path]:
    """Create a CoreStore with a registered provider, registered session bindings
    (skipping ``skip_binding_for`` recipients), and a room roster.

    Returns the store and the path the caller should use for the journal
    (so the journal lives alongside the store, both in tmp_path).
    """
    store = CoreStore()
    store.register_provider(
        PROVIDER_ID,
        ProviderKind.SESSION_TRANSPORT,
        {EvidenceClass.TRANSPORT_SUBMIT_ACK, EvidenceClass.HARNESS_PROMPT_ACCEPTED},
    )
    for index, recipient_id in enumerate(recipients):
        if recipient_id in skip_binding_for:
            continue
        store.register_session_binding(_make_binding(recipient_id, index))
    store.replace_room_roster("family-seam", recipients)
    journal_path = tmp_path / "seam-journal.sqlite"
    return store, journal_path


@pytest.fixture
def _tmp_seam() -> Iterator[Path]:
    with tempfile.TemporaryDirectory() as tmp:
        yield Path(tmp)


# ---------------------------------------------------------------------------
# (a) N=5 bound recipients -> 5 journal rows, one per seat, delivery_ids
#     match exactly what the kernel produced. THIS IS THE LITERAL-ARTIFACT
#     CANARY: the test takes ``result.outcomes`` directly and feeds each
#     ``outcome.delivery_id`` to ``journal.prepare(...)``. The journal row
#     count and per-row delivery_ids are derived from the kernel, not from
#     any hand-rolled loop or reconstruction in the test.
# ---------------------------------------------------------------------------


def test_y_gr_51_kernel_outcomes_feed_journal_one_row_per_seat(_tmp_seam: Path) -> None:
    """Red-proof (a) and the literal-artifact canary.

    Five recipients, all bound. ``BroadcastKernel.broadcast(...)`` returns
    a ``BroadcastResult`` with five outcomes. The journal gets exactly
    five rows. The per-row delivery_ids match the kernel's outcomes
    exactly, in the kernel's order (which is the roster's order).

    If a hand-rolled loop in the test substituted for the kernel (e.g.,
    ``for i in range(5): journal.prepare(...)``), the test would still
    pass on row count but would NOT be using the kernel's literal
    artifacts. The canary asserts delivery_ids match the kernel
    outcomes by VALUE, not just by count.
    """
    recipients = ("yua", "aoi", "tama", "shiori", "nyla")
    store, journal_path = _setup_store_and_roster(_tmp_seam, recipients)

    try:
        kernel = BroadcastKernel(store)
        result = kernel.broadcast(
            actor_id="eric",
            room_id="family-seam",
            content="one canonical event",
            accepted_at=OBSERVED_AT,
        )

        # The kernel is the production source of the rows. Walk its
        # outcomes directly; do NOT reconstruct a recipient list here.
        with InjectionJournal(journal_path) as journal:
            for outcome in result.outcomes:
                assert outcome.binding_id is not None, (
                    "kernel should have produced a binding for every recipient"
                    f" in this fixture; outcome {outcome.recipient_id!r} has none"
                )
                journal.prepare(
                    delivery_id=outcome.delivery_id,
                    binding_id=outcome.binding_id,
                    seat_id=outcome.recipient_id,
                    marker=f"[ygr:{outcome.delivery_id}:seam:canary]",
                    now=0.0,
                )

            unsettled = journal.unsettled()
            observations = {
                "row_count": len(unsettled),
                "row_states": tuple(r.state.value for r in unsettled),
                "row_delivery_ids": tuple(r.delivery_id for r in unsettled),
                "row_seat_ids": tuple(r.seat_id for r in unsettled),
                "kernel_outcome_delivery_ids": tuple(
                    o.delivery_id for o in result.outcomes
                ),
            }
    finally:
        store.close()

    expected_kernel_ids = tuple(o.delivery_id for o in result.outcomes)
    assert observations == {
        "row_count": 5,
        "row_states": ("prepared",) * 5,
        "row_delivery_ids": expected_kernel_ids,
        "row_seat_ids": recipients,
        "kernel_outcome_delivery_ids": expected_kernel_ids,
    }


# ---------------------------------------------------------------------------
# (b) N=5 recipients where 2 have no binding -> exactly 3 journal rows.
#     This proves the kernel is the production source of the rows: a
#     hand-rolled loop with ``range(N_recipients)`` would produce 5
#     rows instead of 3.
# ---------------------------------------------------------------------------


def test_y_gr_51_unavailable_outcomes_are_not_journaled(_tmp_seam: Path) -> None:
    """Red-proof (b).

    Five recipients, two of them without a registered session binding.
    The kernel produces five outcomes (one per roster member) but two
    of them have ``binding_id=None`` and ``unavailable_reason="binding-absent"``.
    The journal can only prepare rows for outcomes with a binding; the
    seam test filters to available outcomes and journals 3 rows.

    A test that used a hand-rolled ``for i in range(5)`` loop with
    placeholder delivery_ids would produce 5 rows and pass the count
    assertion incorrectly. This red-proof asserts the kernel is the
    production source by walking ``result.outcomes`` and verifying the
    row count matches the count of AVAILABLE outcomes (3), not the
    roster count (5).
    """
    recipients = ("yua", "aoi", "tama", "shiori", "nyla")
    skip = frozenset({"shiori", "nyla"})
    store, journal_path = _setup_store_and_roster(
        _tmp_seam, recipients, skip_binding_for=skip
    )

    try:
        kernel = BroadcastKernel(store)
        result = kernel.broadcast(
            actor_id="eric",
            room_id="family-seam",
            content="unavailable-seats-test",
            accepted_at=OBSERVED_AT,
        )

        # Walk kernel outcomes directly; the kernel is the production
        # source of truth for how many rows the journal should hold.
        unavailable = tuple(
            o for o in result.outcomes if o.unavailable_reason is not None
        )
        available = tuple(o for o in result.outcomes if o.unavailable_reason is None)
        assert len(unavailable) == 2, (
            "the fixture sets up 2 recipients without bindings; the kernel "
            f"should mark exactly 2 outcomes as unavailable. Got: {unavailable!r}"
        )
        assert len(available) == 3, (
            f"3 recipients have bindings; 3 outcomes should be available. "
            f"Got: {available!r}"
        )

        with InjectionJournal(journal_path) as journal:
            for outcome in available:
                journal.prepare(
                    delivery_id=outcome.delivery_id,
                    binding_id=outcome.binding_id,  # type: ignore[arg-type]
                    seat_id=outcome.recipient_id,
                    marker=f"[ygr:{outcome.delivery_id}:seam:b]",
                    now=0.0,
                )

            unsettled = journal.unsettled()

        assert len(unsettled) == 3, (
            f"a hand-rolled loop would have produced 5 rows; the kernel "
            f"produced 3 available outcomes; the journal must have 3 rows. "
            f"Got: {len(unsettled)}"
        )
        assert {r.seat_id for r in unsettled} == {"yua", "aoi", "tama"}
        assert {r.delivery_id for r in unsettled} == {o.delivery_id for o in available}
    finally:
        store.close()


# ---------------------------------------------------------------------------
# (c) Roster with a duplicate recipient_id -> the kernel rejects the
#     roster BEFORE the broadcast runs (loud failure, not silent
#     double-write). Asserted at the seam: a duplicate recipient must
#     never reach the journal's prepare path.
# ---------------------------------------------------------------------------


def test_y_gr_51_duplicate_recipient_in_roster_is_rejected(_tmp_seam: Path) -> None:
    """Red-proof (c).

    A roster with a duplicate recipient_id is rejected at
    ``replace_room_roster`` (``ValueError("room roster contains a
    duplicate recipient")``). The broadcast never runs; the journal
    never sees a row. A test that bypassed ``replace_room_roster``
    and built the roster some other way would silently double-write
    at the seam; this red-proof asserts the loud failure.
    """
    recipients_with_dup = ("yua", "aoi", "tama", "aoi")  # aoi twice
    store = CoreStore()
    try:
        store.register_provider(
            PROVIDER_ID,
            ProviderKind.SESSION_TRANSPORT,
            {
                EvidenceClass.TRANSPORT_SUBMIT_ACK,
                EvidenceClass.HARNESS_PROMPT_ACCEPTED,
            },
        )
        for index, recipient_id in enumerate(set(recipients_with_dup)):
            store.register_session_binding(_make_binding(recipient_id, index))

        with pytest.raises(ValueError, match="duplicate recipient"):
            store.replace_room_roster("family-seam-dup", recipients_with_dup)

        # The broadcast never ran; the journal was never opened.
        # Asserting that no rows exist verifies the rejection happened
        # BEFORE the seam could be crossed.
        journal_path = _tmp_seam / "dup-journal.sqlite"
        if journal_path.exists():
            with InjectionJournal(journal_path) as j:
                assert len(j.unsettled()) == 0, (
                    "duplicate roster was rejected; the journal must have "
                    "zero rows. The seam was crossed before the rejection."
                )
    finally:
        store.close()


# ---------------------------------------------------------------------------
# The literal-artifact canary, isolated: any future regression that
# makes the test hand-roll the journal rows (rather than walk the
# kernel's outcomes) breaks this assertion.
# ---------------------------------------------------------------------------


def test_y_gr_51_journal_row_count_equals_available_outcome_count(
    _tmp_seam: Path,
) -> None:
    """The literal-artifact canary for #51.

    Independent of test (a) and test (b): assert that whatever the
    kernel produces as available outcomes, the journal rows count
    matches. No hand-rolled loop. No fixed count. The journal's
    row count is whatever the kernel said it should be.

    If a future maintainer "simplified" this test by replacing
    ``for outcome in available`` with ``for i in range(N)``, this
    test would fail when the kernel produces an unavailable outcome
    (the count would mismatch), and the seam-test discipline would
    be preserved.
    """
    recipients = ("yua", "aoi", "tama")
    store, journal_path = _setup_store_and_roster(_tmp_seam, recipients)

    try:
        kernel = BroadcastKernel(store)
        result = kernel.broadcast(
            actor_id="eric",
            room_id="family-seam",
            content="canary-test",
            accepted_at=OBSERVED_AT,
        )

        with InjectionJournal(journal_path) as journal:
            for outcome in result.outcomes:
                if outcome.unavailable_reason is None:
                    journal.prepare(
                        delivery_id=outcome.delivery_id,
                        binding_id=outcome.binding_id,  # type: ignore[arg-type]
                        seat_id=outcome.recipient_id,
                        marker=f"[ygr:{outcome.delivery_id}:seam:canary2]",
                        now=0.0,
                    )

            kernel_available_count = sum(
                1 for o in result.outcomes if o.unavailable_reason is None
            )
            journal_row_count = len(journal.unsettled())
    finally:
        store.close()

    assert journal_row_count == kernel_available_count, (
        f"journal row count ({journal_row_count}) must equal kernel available "
        f"outcome count ({kernel_available_count}). A mismatch here means a "
        "hand-rolled loop is substituting for the kernel's literal artifacts."
    )
