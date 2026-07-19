"""Y-CMUX-002 — `transport-submitted` requires BOTH events (true negative,
HOLD-not-requeue on UNKNOWN).

Adversarial tracer. The predecessor to this plugin (legacy `agent-bridge
verify_submitted`) scraped the composer and assumed submitted on scrape failure,
silently losing 9+ messages. The native pair `surface.input_sent` +
`workspace.prompt.submitted` is the true-negative substitute; the bus-observer
three-outcome contract (SUBMITTED / NOT_SUBMITTED / UNKNOWN) is what makes
the regression class fail loudly.

This file is the C1 tracer for issue #1; it alone gates build-open. The
injector (``yatagarasu_cmux.injector.Injector``) and the marker
(``yatagarasu_cmux.marker.mint`` / ``extract``) implement the contract
this test asserts against. Citations reference stable symbols, not
commit SHAs — a SHA citation rots on every squash or rebase, and
this file has been rebased twice today already.

Adversarial shape: a `_Observer` that yields ONLY `EVENT_INPUT_SENT`
(simulates a busy pane that accepted the input but did not submit within the
window). The injector must:
  - NOT report SUBMITTED (because the second event is missing).
  - Return UNKNOWN (NOT NOT_SUBMITTED — UNKNOWN means some event was
    observed; we know an event was observed, namely input_sent).
  - The pane must NOT have been touched by a follow-up send_text on the
    `UNKNOWN` path (the duplicate-turn prevention property is the load-bearing
    test here, even though it is enforced at the journal layer in
    Y-CMUX-017; this test asserts the trigger that surfaces UNKNOWN).

Reopen condition (SEV-1): plugin reports `transport-submitted = true` while
`workspace.prompt.submitted` is absent.

Helpers (`_Resolver`, `_Transport`, `_Observer`) are local on purpose:
importing them from `test_injector.py` would couple tests across files,
and the team convention (see test_acceptance_006_010.py) is local helpers.
"""

from __future__ import annotations

from collections.abc import Iterable

from yatagarasu_cmux import (
    EVENT_INPUT_SENT,
    EVENT_PROMPT_SUBMITTED,
    Injector,
    Marker,
    SubmitOutcome,
    extract,
)
from yatagarasu_cmux.outcome import SubmitResult

SIGNING_KEY = b"acceptance-only-signing-key"


class _Resolver:
    def resolve(self, identity: str) -> str:
        return "surface:acceptance"


class _Transport:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []
        self.submitted: list[str] = []

    def send_text(self, surface: str, text: str) -> None:
        self.sent.append((surface, text))

    def submit(self, surface: str) -> None:
        self.submitted.append(surface)


class _Observer:
    """Yields a scripted event chain exactly once, like the host bus.

    The list is fully consumed within the timeout window on the first
    call; once empty, the bus has nothing more to deliver and the
    injector classifies the shortfall. A second call would see an
    empty event list and the injector would classify as NOT_SUBMITTED —
    matching the production semantics where the bus does not replay
    events on re-subscribe.
    """

    def __init__(self, events: list[str]) -> None:
        self._events = list(events)

    def observe(self, marker: Marker, timeout_s: float) -> Iterable[str]:
        while self._events:
            yield self._events.pop(0)


def _build_injector(events: list[str], transport: _Transport | None = None) -> Injector:
    return Injector(
        resolver=_Resolver(),
        transport=transport or _Transport(),
        observer=_Observer(events),
        signing_key=SIGNING_KEY,
        submit_timeout_s=0.05,
    )


def test_y_cmux_002_input_sent_without_submit_is_unknown_holds_no_requeue() -> None:
    """Adversarial fixture: input accepted, submit suppressed.

    The injector must return UNKNOWN (some event was observed, namely
    `surface.input_sent`; this is the busy-pane-pending case, not the
    transport-dropped case). UNKNOWN's contract is `must_hold` and
    `not may_requeue` — the test asserts both.
    """
    inj = _build_injector(events=[EVENT_INPUT_SENT])

    result: SubmitResult = inj.deliver("peer", "d-001", "hello world")

    # Tracer contract assertions:
    assert result.outcome is SubmitOutcome.UNKNOWN, (
        f"outcome must be UNKNOWN for input-sent-without-submit, got {result.outcome!r}. "
        f"This is the regression to the lenient-scrape pattern."
    )
    assert result.must_hold, "UNKNOWN must_hold invariant broken"
    assert not result.may_requeue, (
        "UNKNOWN may_requeue invariant broken — requeue here would race "
        "the busy-queue admission and create a duplicate turn."
    )
    # SEV-1 reopen condition: the predecessor assumed submitted on scrape
    # failure. The injector must NOT report SUBMITTED in this fixture.
    assert not result.is_proven, (
        "is_proven must be False when only input_sent was observed; "
        "this is the SEV-1 reopen condition."
    )
    # The literal event chain must include exactly what the bus saw.
    assert result.source_events == (EVENT_INPUT_SENT,)


def test_y_cmux_002_no_events_at_all_is_clean_negative_may_requeue() -> None:
    """NOT_SUBMITTED is the safe-to-requeue case (no events at all observed).

    This is the second half of the three-outcome contract: when the input
    never reached the host, there is no busy-queue admission to race, and
    requeue is safe. The test asserts the `may_requeue=True` invariant.
    """
    inj = _build_injector(events=[])

    result = inj.deliver("peer", "d-002", "hello world")

    assert result.outcome is SubmitOutcome.NOT_SUBMITTED, (
        f"outcome must be NOT_SUBMITTED when nothing was observed, got {result.outcome!r}."
    )
    assert result.may_requeue, (
        "NOT_SUBMITTED may_requeue invariant broken — the busy-pane case "
        "is impossible when no events were observed."
    )
    assert not result.is_proven
    assert result.source_events == ()


def test_y_cmux_002_both_events_prove_submission() -> None:
    """The positive control: both events observed → SUBMITTED.

    This is the regression test's negative-control counterpart — if the
    bus delivers both events, the injector must report SUBMITTED. The
    tests above prove the negative space; this one proves the positive.
    Together they bound the three-outcome decision tree.
    """
    inj = _build_injector(events=[EVENT_INPUT_SENT, EVENT_PROMPT_SUBMITTED])

    result = inj.deliver("peer", "d-003", "hello world")

    assert result.outcome is SubmitOutcome.SUBMITTED
    assert result.is_proven
    assert not result.must_hold
    assert not result.may_requeue
    assert result.source_events == (EVENT_INPUT_SENT, EVENT_PROMPT_SUBMITTED)


def test_y_cmux_002_marker_is_recoverable_from_sent_text_on_unknown() -> None:
    """On UNKNOWN the marker is still in the sent text and recoverable.

    Y-CMUX-013 says session_id alone is insufficient; the marker is the
    load-bearing correlation. This test asserts the marker survives the
    input_sent-only fixture, so the journal layer (Y-CMUX-017) can
    reconcile the marker against the bus evidence at recovery time.
    """
    transport = _Transport()
    inj = Injector(
        resolver=_Resolver(),
        transport=transport,
        observer=_Observer([EVENT_INPUT_SENT]),
        signing_key=SIGNING_KEY,
        submit_timeout_s=0.05,
    )

    result = inj.deliver("peer", "d-004", "payload body")

    assert result.outcome is SubmitOutcome.UNKNOWN
    assert transport.sent, (
        "the input_sent path must have sent at least one message; "
        "otherwise the fixture is wrong."
    )
    _, sent_text = transport.sent[0]
    # Extract the actual marker from the sent text (NOT a fresh `mint()` —
    # mint returns a new nonce/signature each call, so a second mint would
    # never match the embedded one). The whole point of the marker is that
    # it's recoverable from the host event payload.
    recovered = extract(SIGNING_KEY, sent_text)
    assert recovered is not None, (
        f"marker must be extractable from sent text {sent_text!r}; "
        "the journal layer relies on this for crash-window reconciliation."
    )
    assert recovered.delivery_id == "d-004", (
        f"recovered marker must carry the original delivery_id; "
        f"got {recovered.delivery_id!r}, expected 'd-004'. "
        "If the marker text does not match the key, the signer is wrong."
    )


def test_y_cmux_002_observer_consumes_event_chain_on_first_call() -> None:
    """The fake observer's events are consumed; a second call sees empty.

    This test asserts the docstring claim — "exactly once" — matches
    behaviour. A naïve `yield from self._events` would replay the chain
    on every call, masking a real bug in production where the bus does
    not re-send events on re-subscribe. The pop() loop enforces
    consumption; the second call sees an empty list and the injector
    classifies as NOT_SUBMITTED.
    """
    transport = _Transport()
    observer = _Observer([EVENT_INPUT_SENT, EVENT_PROMPT_SUBMITTED])
    inj = Injector(
        resolver=_Resolver(),
        transport=transport,
        observer=observer,
        signing_key=SIGNING_KEY,
        submit_timeout_s=0.05,
    )

    first = inj.deliver("peer", "d-005", "first body")
    assert first.outcome is SubmitOutcome.SUBMITTED, (
        "first call must see both events; the chain is still populated"
    )

    # The transport was used for the first send; reset it so we can
    # observe the second-call outcome without that side-channel noise.
    transport.sent.clear()
    transport.submitted.clear()

    second = inj.deliver("peer", "d-006", "second body")
    # The observer's event chain has been consumed; the second call sees
    # an empty list, the injector classifies as NOT_SUBMITTED.
    assert second.outcome is SubmitOutcome.NOT_SUBMITTED, (
        "second call must see an empty event chain (consumed-on-first-call) "
        "and classify as NOT_SUBMITTED, NOT SUBMITTED. If this fails with "
        "SUBMITTED, the fake is replaying events on every call — a regression "
        "of the bus-replay shape we are trying to test against."
    )
