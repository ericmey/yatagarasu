"""Inject a message into a live session and prove — or disprove — that it landed.

This is the minimal transport slice the C1 tracer asserts against.

Two rules shape the whole module:

1. **Address by identity, resolve every send.** Surface handles are ephemeral.
   They change when a session restarts, and a stale handle delivers into a dead or
   reassigned surface while returning success locally. Nothing here caches a
   handle between sends.

2. **Both events, or it did not happen.** ``transport-submitted`` requires the
   host to confirm *both* that our input was accepted and that the composer
   actually submitted. Seeing only the first is not partial success; it is not
   success.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Protocol

from .marker import Marker, MarkerError, mint, redact
from .outcome import SubmitOutcome, SubmitResult

log = logging.getLogger(__name__)

#: Host event proving the socket accepted our input.
EVENT_INPUT_SENT = "surface.input_sent"
#: Host event proving the composer actually submitted a prompt.
EVENT_PROMPT_SUBMITTED = "workspace.prompt.submitted"

REQUIRED_EVENTS: tuple[str, str] = (EVENT_INPUT_SENT, EVENT_PROMPT_SUBMITTED)


class ResolutionError(RuntimeError):
    """Raised when an identity cannot be resolved to a live surface."""


class Resolver(Protocol):
    """Maps an identity to a live surface handle, freshly, every call."""

    def resolve(self, identity: str) -> str: ...


class Transport(Protocol):
    """Places text into a surface and submits it."""

    def send_text(self, surface: str, text: str) -> None: ...

    def submit(self, surface: str) -> None: ...


class BusObserver(Protocol):
    """Yields host event names correlated to a marker.

    Implementations must not block the caller indefinitely; they are expected to
    stop yielding once their own deadline passes so the caller can decide between
    a true negative and an ambiguous outcome.
    """

    def observe(self, marker: Marker, timeout_s: float) -> Iterable[str]: ...


@dataclass(slots=True)
class Injector:
    """Delivers one message into one identity's live session."""

    resolver: Resolver
    transport: Transport
    observer: BusObserver
    signing_key: bytes
    submit_timeout_s: float = 10.0
    #: Called with ``(delivery_id, surface)`` immediately BEFORE the local effect
    #: is attempted — the pane has not been touched yet when this fires. The
    #: journal writes its intent record here so that a crash between this point
    #: and confirmation is recoverable as ambiguous rather than invisible. If it
    #: were called after the effect, a crash in between would leave no record and
    #: recovery would read "never injected".
    on_effect_pending: Callable[[str, str], None] | None = None

    def deliver(self, identity: str, delivery_id: str, body: str) -> SubmitResult:
        """Inject ``body`` for ``identity`` and report a definite outcome.

        Never returns an optimistic result. If the required evidence does not
        arrive, the outcome is a true negative or an explicit unknown.
        """
        # Minting can reject a bad key or delivery_id. That happens before any
        # local effect, so it is a clean negative, not an ambiguity — and this
        # method promises a verdict, never an exception.
        try:
            marker = mint(self.signing_key, delivery_id)
        except MarkerError as exc:
            log.warning("marker mint failed delivery=%s", delivery_id)
            return SubmitResult(
                SubmitOutcome.NOT_SUBMITTED, delivery_id, (), f"marker error: {exc}"
            )

        # Resolved per send, never cached: a handle from a previous send may now
        # point at a surface that no longer exists.
        try:
            surface = self.resolver.resolve(identity)
        except Exception as exc:
            # Absence is visible, not a silent drop, and nothing was injected, so
            # this is a clean negative rather than an ambiguity.
            log.warning("resolve failed identity=%s delivery=%s", identity, delivery_id)
            return SubmitResult(
                SubmitOutcome.NOT_SUBMITTED,
                delivery_id,
                (),
                f"unresolved: {exc}",
            )

        text = f"{marker.text} {body}"

        # From here the local effect may fire. Record intent BEFORE touching the
        # surface: a crash between the effect and the journal write would
        # otherwise look like "never injected" and invite a duplicate turn.
        if self.on_effect_pending is not None:
            self.on_effect_pending(delivery_id, surface)

        try:
            self.transport.send_text(surface, text)
            self.transport.submit(surface)
        except Exception as exc:
            # The send may have partially applied. We cannot prove it did not.
            log.warning(
                "transport raised, outcome ambiguous delivery=%s body=%s",
                delivery_id,
                redact(body),
            )
            return SubmitResult(
                SubmitOutcome.UNKNOWN, delivery_id, (), f"transport error: {exc}"
            )

        return self._await_proof(marker, delivery_id)

    def _await_proof(self, marker: Marker, delivery_id: str) -> SubmitResult:
        seen: list[str] = []
        for name in self.observer.observe(marker, self.submit_timeout_s):
            if name not in seen:
                seen.append(name)
            if all(required in seen for required in REQUIRED_EVENTS):
                return SubmitResult(SubmitOutcome.SUBMITTED, delivery_id, tuple(seen))

        return self._classify_shortfall(delivery_id, seen)

    @staticmethod
    def _classify_shortfall(delivery_id: str, seen: Sequence[str]) -> SubmitResult:
        """Decide between a true negative and an ambiguity.

        Nothing observed at all means the input never reached the host: a clean
        negative, safe to re-queue.

        Input accepted but no submit observed is **not** the same thing. The
        composer may hold text that submits later, so re-queuing could duplicate
        the turn. That is held as unknown and surfaced for reconciliation.
        """
        if not seen:
            return SubmitResult(
                SubmitOutcome.NOT_SUBMITTED,
                delivery_id,
                (),
                "no host events observed within window",
            )

        if EVENT_INPUT_SENT in seen and EVENT_PROMPT_SUBMITTED not in seen:
            return SubmitResult(
                SubmitOutcome.UNKNOWN,
                delivery_id,
                tuple(seen),
                "input accepted but submit unobserved; holding to avoid a duplicate turn",
            )

        return SubmitResult(
            SubmitOutcome.UNKNOWN,
            delivery_id,
            tuple(seen),
            "incomplete evidence chain",
        )
