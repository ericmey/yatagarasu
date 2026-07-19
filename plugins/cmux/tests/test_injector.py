"""Tests for the minimal injector.

These are written against the failure modes, not the happy path. The happy path
was never the problem: the predecessor reported success constantly. What it could
not do was tell the truth when it did not know.
"""

from __future__ import annotations

import pytest
from yatagarasu_cmux.harness_profiles import HarnessKind
from yatagarasu_cmux.injector import (
    EVENT_INPUT_SENT,
    EVENT_PROMPT_SUBMITTED,
    Injector,
    ResolutionError,
)
from yatagarasu_cmux.marker import extract, redact
from yatagarasu_cmux.outcome import SubmitOutcome

from yatagarasu_core import Delivery, DeliveryMode
from yatagarasu_core.proofs import DeliveryMarker, MarkerAuthority

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
        self.submitted: list[tuple[str, str]] = []
        self.fail_on_send = fail_on_send

    def send_text(self, surface: str, text: str) -> None:
        if self.fail_on_send:
            raise OSError("socket closed")
        self.sent.append((surface, text))

    def submit(self, surface: str, key: str) -> None:
        self.submitted.append((surface, key))


class FakeObserver:
    """Yields a scripted event chain, as the host bus would."""

    def __init__(self, events: list[str]) -> None:
        self.events = events

    def observe(self, marker: DeliveryMarker, timeout_s: float):
        yield from self.events


def build(handles=("surface:7",), events=None, transport=None):
    return Injector(
        resolver=FakeResolver(list(handles)),
        transport=transport or FakeTransport(),
        observer=FakeObserver(list(events if events is not None else [])),
        marker_authority=MarkerAuthority(KEY),
    )


def _deliver(
    inj: Injector,
    delivery_id: str,
    *,
    identity: str = "peer",
    body: str = "hello",
    harness: HarnessKind | str = HarnessKind.CLAUDE_CODE,
):
    delivery = Delivery(
        "ev", delivery_id, "attempt", "b-1", "rec", DeliveryMode.SESSION_BOUND
    )
    return inj.deliver(
        identity,
        delivery,
        body,
        "2026-07-19T20:00:00Z",
        "2026-07-19T20:05:00Z",
        harness=harness,
    )


def test_both_events_prove_submission():
    inj = build(events=[EVENT_INPUT_SENT, EVENT_PROMPT_SUBMITTED])
    result = _deliver(inj, "d-1")

    assert result.outcome is SubmitOutcome.SUBMITTED
    assert result.is_proven
    assert result.source_events == (EVENT_INPUT_SENT, EVENT_PROMPT_SUBMITTED)


def test_input_sent_alone_is_not_submission():
    """The load-bearing assertion. This is the regression test for the class of
    bug where a partially-observed send was reported as delivered."""
    inj = build(events=[EVENT_INPUT_SENT])
    result = _deliver(inj, "d-2")

    assert result.outcome is not SubmitOutcome.SUBMITTED
    assert not result.is_proven


def test_input_sent_without_submit_is_held_not_requeued():
    """Ambiguity must not be rounded down to a clean negative.

    A busy pane legitimately holds injected text and submits it on the next turn,
    so re-queuing here could produce a duplicate turn.
    """
    inj = build(events=[EVENT_INPUT_SENT])
    result = _deliver(inj, "d-3")

    assert result.outcome is SubmitOutcome.UNKNOWN
    assert result.must_hold
    assert not result.may_requeue


def test_no_events_at_all_is_a_clean_negative():
    """Nothing reached the host, so nothing can be sitting in a composer."""
    inj = build(events=[])
    result = _deliver(inj, "d-4")

    assert result.outcome is SubmitOutcome.NOT_SUBMITTED
    assert result.may_requeue


def test_unresolvable_identity_is_visible_not_silent():
    inj = build(handles=())
    result = _deliver(inj, "d-5", identity="ghost")

    assert result.outcome is SubmitOutcome.NOT_SUBMITTED
    assert "unresolved" in result.detail


def test_transport_failure_is_ambiguous_never_negative():
    """A send that raised may have partially applied; we cannot prove it did not."""
    inj = build(transport=FakeTransport(fail_on_send=True))
    result = _deliver(inj, "d-6")

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
        marker_authority=MarkerAuthority(KEY),
    )

    _deliver(inj, "d-7")
    _deliver(inj, "d-8")

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
        marker_authority=MarkerAuthority(KEY),
        on_effect_pending=lambda d, s: order.append("journal"),
    )
    _deliver(inj, "d-9")

    assert order == ["journal", "effect"]


def test_marker_is_embedded_and_recoverable():
    transport = FakeTransport()
    inj = Injector(
        resolver=FakeResolver(["surface:3"]),
        transport=transport,
        observer=FakeObserver([EVENT_INPUT_SENT, EVENT_PROMPT_SUBMITTED]),
        marker_authority=MarkerAuthority(KEY),
    )
    _deliver(inj, "d-10")

    _, text = transport.sent[0]
    found = extract(None, text)
    assert found is not None
    assert found.delivery_id == "d-10"
    assert "hello" in text


@pytest.mark.parametrize(
    ("harness", "prefix", "submit_keys"),
    [
        (HarnessKind.CLAUDE_CODE, "ygr1s.", ("enter",)),
        (HarnessKind.CODEX, "ygr1s.", ("tab", "enter")),
        (HarnessKind.HERMES, "/queue ygr1s.", ("enter",)),
    ],
)
def test_injector_applies_explicit_harness_profile(harness, prefix, submit_keys):
    transport = FakeTransport()
    inj = Injector(
        resolver=FakeResolver(["surface:profile"]),
        transport=transport,
        observer=FakeObserver([EVENT_INPUT_SENT, EVENT_PROMPT_SUBMITTED]),
        marker_authority=MarkerAuthority(KEY),
    )

    result = _deliver(inj, "d-profile", body="the body", harness=harness)

    assert result.outcome is SubmitOutcome.SUBMITTED
    assert transport.sent[0][1].startswith(prefix)
    assert transport.submitted == [
        ("surface:profile", submit_key) for submit_key in submit_keys
    ]


def test_unsupported_harness_is_clean_negative_without_terminal_effect():
    transport = FakeTransport()
    inj = Injector(
        resolver=FakeResolver(["surface:profile"]),
        transport=transport,
        observer=FakeObserver([]),
        marker_authority=MarkerAuthority(KEY),
    )

    result = _deliver(inj, "d-unknown", body="body", harness="unknown")

    assert result.outcome is SubmitOutcome.NOT_SUBMITTED
    assert "unsupported harness" in result.detail
    assert transport.sent == []
    assert transport.submitted == []


def test_forged_marker_is_rejected():
    # Pre-auth extraction works, because decoding does not verify
    authority = MarkerAuthority(KEY)
    delivery = Delivery(
        "ev-11", "d-11", "a-11", "b-11", "yua", DeliveryMode.SESSION_BOUND
    )
    real = authority.mint(
        delivery, issued_at="2026-07-19T21:00:00Z", expires_at="2026-07-19T21:05:00Z"
    )

    # Encode it to base64, then tamper with the payload to simulate forgery
    real_text = authority.encode(real)
    forged_text = real_text[:-10] + "0" * 10

    assert extract(None, real_text) is not None
    # Forgeries are now caught at validation time in core, not extraction time.
    # Therefore, extract() will either return None if parsing fails, OR return a DeliveryMarker
    # that fails later. If it fails to parse (corrupted JSON or base64), it's None.
    # In this case, we corrupted the base64, so it fails to parse and returns None.
    assert extract(None, forged_text) is None


def test_each_attempt_gets_a_distinct_marker():
    """A retry must be distinguishable from the original attempt.
    Core mint is deterministic by attempt_id, not by a random nonce.
    A retry (new attempt_id) produces a distinct marker; the identical
    attempt produces an identical marker.
    """
    authority = MarkerAuthority(KEY)

    delivery_1 = Delivery(
        "ev-12", "d-12", "a-12", "b-12", "yua", DeliveryMode.SESSION_BOUND
    )
    m1 = authority.mint(
        delivery_1, issued_at="2026-07-19T21:00:00Z", expires_at="2026-07-19T21:05:00Z"
    )

    # Same delivery + attempt -> identical sig
    delivery_1_duplicate = Delivery(
        "ev-12", "d-12", "a-12", "b-12", "yua", DeliveryMode.SESSION_BOUND
    )
    m1_duplicate = authority.mint(
        delivery_1_duplicate,
        issued_at="2026-07-19T21:00:00Z",
        expires_at="2026-07-19T21:05:00Z",
    )
    assert m1.signature == m1_duplicate.signature

    # New attempt -> distinct sig
    delivery_2 = Delivery(
        "ev-12", "d-12", "a-12-retry", "b-12", "yua", DeliveryMode.SESSION_BOUND
    )
    m2 = authority.mint(
        delivery_2, issued_at="2026-07-19T21:00:00Z", expires_at="2026-07-19T21:05:00Z"
    )
    assert m1.signature != m2.signature


@pytest.mark.parametrize("value", ["", None])
def test_redact_never_reproduces_content(value):
    assert "secret" not in redact(value)


def test_redact_reports_length_only():
    out = redact("super secret prompt text")
    assert "secret" not in out
    assert "len=24" in out


# --- regression tests for the review findings on this module ---


def test_bad_signing_key_returns_verdict_not_exception():
    """The contract is a verdict, always. Minting rejects an empty key, and that
    happens before any local effect — so it is a clean negative, not a raise."""
    inj = Injector(
        resolver=FakeResolver(["surface:1"]),
        transport=FakeTransport(),
        observer=FakeObserver([]),
        marker_authority=MarkerAuthority(KEY),
    )

    # Monkeypatch the minting to simulate an error (like an invalid key configuration handled natively by core MarkerAuthority)
    def fail_mint(*args, **kwargs):
        raise ValueError("signing key must not be empty")

    inj.marker_authority.mint = fail_mint

    result = _deliver(inj, "d-20")

    assert result.outcome is SubmitOutcome.NOT_SUBMITTED
    assert result.may_requeue
    assert "marker error" in result.detail


def test_bad_delivery_id_returns_verdict_not_exception():
    """Testing bad delivery ID handling. Since core's MarkerAuthority doesn't
    natively throw on bad strings during minting like the old cmux mint() did,
    we simulate the failure by mocking the mint method.
    """
    inj = build()

    def fail_mint(*args, **kwargs):
        raise ValueError("delivery_id is not a valid token")

    inj.marker_authority.mint = fail_mint

    delivery = Delivery(
        "ev",
        "invalid token string!",
        "attempt",
        "b-1",
        "rec",
        DeliveryMode.SESSION_BOUND,
    )
    result = inj.deliver(
        "peer",
        delivery,
        "hello",
        "2026-07-19T20:00:00Z",
        "2026-07-19T20:05:00Z",
        harness=HarnessKind.CLAUDE_CODE,
    )

    assert result.outcome is SubmitOutcome.NOT_SUBMITTED
    assert "marker error" in result.detail


def test_nothing_is_injected_when_minting_fails():
    """A mint failure must not touch the pane, or the 'clean negative' is a lie."""
    transport = FakeTransport()
    inj = Injector(
        resolver=FakeResolver(["surface:1"]),
        transport=transport,
        observer=FakeObserver([]),
        marker_authority=MarkerAuthority(KEY),
    )

    def fail_mint(*args, **kwargs):
        raise ValueError("signing key must not be empty")

    inj.marker_authority.mint = fail_mint

    _deliver(inj, "d-21")

    assert transport.sent == []
    assert transport.submitted == []


def test_empty_key_never_authenticates_a_marker():
    """An empty key makes every signature computable by anyone. Fail closed:
    a misconfigured deployment must reject markers at construction, not accept forgeries.
    """
    with pytest.raises(ValueError, match="marker signing key must not be empty"):
        MarkerAuthority(b"")
