from __future__ import annotations

import contextlib
import dataclasses
import re
from collections.abc import Callable

from yatagarasu_core import (
    Delivery,
    DeliveryMarker,
    Disposition,
    EvidenceClass,
    Receipt,
    SessionProof,
    SourceEventRef,
)
from yatagarasu_core.proofs import MarkerAuthority

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
        delivery_lookup: Callable[[str], tuple[Delivery, DeliveryMarker] | None],
    ) -> None:
        self._core = core_client
        self._provider_id = provider_id
        self._delivery_lookup = delivery_lookup

        # session_id -> (Delivery, DeliveryMarker, [input_sent, prompt_submitted, user_prompt_submit])
        self._active_chains: dict[
            str, tuple[Delivery, DeliveryMarker, list[SourceEventRef]]
        ] = {}

        # Ephemeral buffer for the current turn being built
        self._pending_input: SourceEventRef | None = None
        self._pending_prompt: SourceEventRef | None = None
        self._pending_delivery_id: str | None = None

    def observe(
        self, event: SourceEventRef, payload: dict | None = None, observed_at: str = ""
    ) -> None:
        """Process an event from the cmux event bus."""
        name = event.event_name

        if name == "surface.input_sent":
            self._pending_input = event

        elif name == "workspace.prompt.submitted":
            self._pending_prompt = event
            if payload and "message_preview" in payload:
                match = _YGR1_RE.search(payload["message_preview"])
                if match:
                    with contextlib.suppress(Exception):
                        decoded = MarkerAuthority.decode(match.group(1))
                        self._pending_delivery_id = decoded.delivery_id
        elif name == "agent.hook.UserPromptSubmit":
            try:
                if not event.session_id:
                    return

                if (
                    self._pending_input
                    and self._pending_prompt
                    and self._pending_delivery_id
                ):
                    context = self._delivery_lookup(self._pending_delivery_id)
                    if context:
                        delivery, core_marker = context
                        # Populate required durable correlation fields onto the SourceEventRef
                        prompt_event = dataclasses.replace(
                            self._pending_prompt,
                            binding_id=delivery.binding_id,
                            marker_signature=core_marker.signature,
                        )

                        chain = [self._pending_input, prompt_event, event]
                        self._active_chains[event.session_id] = (
                            delivery,
                            core_marker,
                            chain,
                        )
            finally:
                # Clear ephemeral buffer on every exit path
                self._pending_input = None
                self._pending_prompt = None
                self._pending_delivery_id = None

        elif name == "agent.hook.Stop":
            if not event.session_id:
                return

            chain_data = self._active_chains.pop(event.session_id, None)
            if not chain_data:
                # Uncorrelated Stop: emit nothing.
                return

            delivery, core_marker, previous_events = chain_data
            source_events = tuple([*previous_events, event])

            proof = SessionProof(
                session_id=event.session_id,
                marker=core_marker,
                source_events=source_events,
            )

            receipt = Receipt(
                receipt_id=f"rec-{event.source_event_id}",
                event_id=delivery.event_id,
                delivery_id=delivery.delivery_id,
                attempt_id=delivery.attempt_id,
                binding_id=delivery.binding_id,
                evidence_provider_id=self._provider_id,
                evidence_class=EvidenceClass.HARNESS_TURN_COMPLETED,
                proof_method="cmux.event_bus.harness_hook_relay",
                observed_at=observed_at,
                source_event_id=event.source_event_id,
                disposition=Disposition.COMPLETED,
                proof=proof,
            )

            self._core(receipt)
