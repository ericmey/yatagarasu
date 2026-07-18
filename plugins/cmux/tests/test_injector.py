"""Tests for the minimal injector.

These are written against the failure modes, not the happy path. The happy path
was never the problem: the predecessor reported success constantly. What it could
not do was tell the truth when it did not know.
"""

from __future__ import annotations

import pytest
from yatagarasu_cmux.injector import (
    EVENT_INPUT_SENT,
    EVENT_PROMPT_SUBMITTED,
    Injector,
    ResolutionError,
)
from yatagarasu_cmux.marker import Marker, extract, mint, redact
from yatagarasu_cmux.outcome import SubmitOutcome

KEY = b"test-signing-key"


class FakeResolver:
    """Records every resolve so we can prove no handle is ever cached."""

    def __init__(self, handles: list[str]) -> None:
        self._handles = list(handles)
        self.calls: list[str] = []

    def resolve(self, identity: str) -> str:
        self.calls.append(identity)
        if not self._handles:
            raise ResolutionError(f"no live surface for {identity}")
        return self._handles.pop(0)


class FakeTransport:
    def __init__(self, fail_on_send: bool = False) -> None:
        self.sent: list[tuple[str, str]] = []
        self.submitted: list[str] = []
        self.fail_on_send = fail_on_send

    def send_text(self, surface: str, text: str) -> None:
        if self.fail_on_send:
            raise OSError("socket closed")
        self.sent.append((surface, text))

    def submit(self, surface: str) -> None:
        self.submitted.append(surface)


class FakeObserver:
    """Yields a scripted event chain, as the host bus would."""

    def __init__(self, events: list[str]) -> None:
        self.events = events

    def observe(self, marker: Marker, timeout_s: float):
        yield from self.events


def build(handles=("surface:7",), events=None, transport=None):
    return Injector(
        resolver=FakeResolver(list(handles)),
        transport=transport or FakeTransport(),
        observer=FakeObserver(list(events if events is not None else [])),
        signing_key=KEY,
    )


def test_both_events_prove_submission():
    inj = build(events=[EVENT_INPUT_SENT, EVENT_PROMPT_SUBMITTED])
    result = inj.deliver("peer", "d-1", "hello")

    assert result.outcome is SubmitOutcome.SUBMITTED
    assert result.is_proven
    assert result.source_events == (EVENT_INPUT_SENT, EVENT_PROMPT_SUBMITTED)


def test_input_sent_alone_is_not_submission():
    """The load-bearing assertion. This is the regression test for the class of
    bug where a partially-observed send was reported as delivered."""
    inj = build(events=[EVENT_INPUT_SENT])
    result = inj.deliver("peer", "d-2", "hello")

    assert result.outcome is not SubmitOutcome.SUBMITTED
    assert not result.is_proven


def test_input_sent_without_submit_is_held_not_requeued():
    """Ambiguity must not be rounded down to a clean negative.

    A busy pane legitimately holds injected text and submits it on the next turn,
    so re-queuing here could produce a duplicate turn.
    """
    inj = build(events=[EVENT_INPUT_SENT])
    result = inj.deliver("peer", "d-3", "hello")

    assert result.outcome is SubmitOutcome.UNKNOWN
    assert result.must_hold
    assert not result.may_requeue


def test_no_events_at_all_is_a_clean_negative():
    """Nothing reached the host, so nothing can be sitting in a composer."""
    inj = build(events=[])
    result = inj.deliver("peer", "d-4", "hello")

    assert result.outcome is SubmitOutcome.NOT_SUBMITTED
    assert result.may_requeue


def test_unresolvable_identity_is_visible_not_silent():
    inj = build(handles=())
    result = inj.deliver("ghost", "d-5", "hello")

    assert result.outcome is SubmitOutcome.NOT_SUBMITTED
    assert "unresolved" in result.detail


def test_transport_failure_is_ambiguous_never_negative():
    """A send that raised may have partially applied; we cannot prove it did not."""
    inj = build(transport=FakeTransport(fail_on_send=True))
    result = inj.deliver("peer", "d-6", "hello")

    assert result.outcome is SubmitOutcome.UNKNOWN
    assert result.must_hold


def test_surface_is_resolved_every_send_never_cached():
    """Handles are ephemeral. A cached handle delivers into a dead surface and
    still looks locally successful, which is the worst failure shape available."""
    resolver = FakeResolver(["surface:1", "surface:2"])
    transport = FakeTransport()
    inj = Injector(
        resolver=resolver,
        transport=transport,
        observer=FakeObserver([EVENT_INPUT_SENT, EVENT_PROMPT_SUBMITTED]),
        signing_key=KEY,
    )

    inj.deliver("peer", "d-7", "one")
    inj.deliver("peer", "d-8", "two")

    assert resolver.calls == ["peer", "peer"]
    assert [s for s, _ in transport.sent] == ["surface:1", "surface:2"]


def test_effect_pending_is_recorded_before_the_effect():
    """The journal's intent record must precede the local effect, or a crash in
    between reads as 'never injected' and invites a duplicate turn."""
    order: list[str] = []

    class OrderedTransport(FakeTransport):
        def send_text(self, surface: str, text: str) -> None:
            order.append("effect")
            super().send_text(surface, text)

    inj = Injector(
        resolver=FakeResolver(["surface:9"]),
        transport=OrderedTransport(),
        observer=FakeObserver([EVENT_INPUT_SENT, EVENT_PROMPT_SUBMITTED]),
        signing_key=KEY,
        on_effect_pending=lambda d, s: order.append("journal"),
    )
    inj.deliver("peer", "d-9", "hello")

    assert order == ["journal", "effect"]


def test_marker_is_embedded_and_recoverable():
    transport = FakeTransport()
    inj = Injector(
        resolver=FakeResolver(["surface:3"]),
        transport=transport,
        observer=FakeObserver([EVENT_INPUT_SENT, EVENT_PROMPT_SUBMITTED]),
        signing_key=KEY,
    )
    inj.deliver("peer", "d-10", "the body")

    _, text = transport.sent[0]
    found = extract(KEY, text)
    assert found is not None
    assert found.delivery_id == "d-10"
    assert "the body" in text


def test_forged_marker_is_rejected():
    real = mint(KEY, "d-11")
    forged = f"[ygr:{real.delivery_id}:{real.nonce}:{'0' * 16}]"

    assert extract(KEY, real.text) is not None
    assert extract(KEY, forged) is None
    assert extract(b"wrong-key", real.text) is None


def test_each_attempt_gets_a_distinct_marker():
    """A retry must be distinguishable from the original attempt."""
    assert mint(KEY, "d-12").nonce != mint(KEY, "d-12").nonce


@pytest.mark.parametrize("value", ["", None])
def test_redact_never_reproduces_content(value):
    assert "secret" not in redact(value)


def test_redact_reports_length_only():
    out = redact("super secret prompt text")
    assert "secret" not in out
    assert "len=24" in out
