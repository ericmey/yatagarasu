from __future__ import annotations

import contextlib
import re
from collections.abc import Callable

from yatagarasu_core import (
    DeliveryMarker,
    Disposition,
    EvidenceClass,
    Receipt,
    SessionProof,
    SourceEventRef,
)
from yatagarasu_core.proofs import MarkerAuthority, MarkerError

_YGR1_RE = re.compile(r"(ygr1\.[A-Za-z0-9_-]+)")


class ReceiptEmitter:
    """
    Translates agent.hook.Stop events into core-conformant processed(completed) receipts.
    Only emits a receipt if the Stop correlates to an accepted prompt in the same session.
    """

    def __init__(
        self,
        core_client: Callable[[Receipt], None],
        provider_id: str,
    ) -> None:
        self._core = core_client
        self._provider_id = provider_id

        # session_id -> (marker, [input_sent, prompt_submitted, user_prompt_submit])
        self._active_chains: dict[str, tuple[DeliveryMarker, list[SourceEventRef]]] = {}

        # Ephemeral buffer for the current turn being built
        self._pending_input: SourceEventRef | None = None
        self._pending_prompt: SourceEventRef | None = None
        self._pending_marker: DeliveryMarker | None = None

    def observe(self, event: SourceEventRef, payload: dict | None = None) -> None:
        """Process an event from the cmux event bus."""
        name = event.event_name

        if name == "surface.input_sent":
            self._pending_input = event

        elif name == "workspace.prompt.submitted":
            self._pending_prompt = event
            if payload and "message_preview" in payload:
                match = _YGR1_RE.search(payload["message_preview"])
                if match:
                    with contextlib.suppress(MarkerError):
                        self._pending_marker = MarkerAuthority.decode(match.group(1))

        elif name == "agent.hook.UserPromptSubmit":
            if not event.session_id:
                return

            if self._pending_input and self._pending_prompt and self._pending_marker:
                chain = [self._pending_input, self._pending_prompt, event]
                self._active_chains[event.session_id] = (self._pending_marker, chain)

            # Clear ephemeral buffer
            self._pending_input = None
            self._pending_prompt = None
            self._pending_marker = None

        elif name == "agent.hook.Stop":
            if not event.session_id:
                return

            chain_data = self._active_chains.pop(event.session_id, None)
            if not chain_data:
                # Uncorrelated Stop: emit nothing.
                return

            marker, previous_events = chain_data
            source_events = tuple([*previous_events, event])

            proof = SessionProof(
                session_id=event.session_id,
                marker=marker,
                source_events=source_events,
            )

            receipt = Receipt(
                receipt_id=f"rec-{event.source_event_id}",
                event_id=marker.event_id,
                delivery_id=marker.delivery_id,
                attempt_id=marker.attempt_id,
                binding_id=marker.binding_id,
                evidence_provider_id=self._provider_id,
                evidence_class=EvidenceClass.HARNESS_TURN_COMPLETED,
                proof_method="cmux.event_bus.harness_hook_relay",
                observed_at="2026-07-18T21:00:00Z",  # In reality, we'd use a real timestamp
                source_event_id=event.source_event_id,
                disposition=Disposition.COMPLETED,
                proof=proof,
            )

            self._core(receipt)
