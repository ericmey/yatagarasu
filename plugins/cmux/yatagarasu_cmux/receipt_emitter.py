from __future__ import annotations

import contextlib
import re
from collections.abc import Callable
from dataclasses import dataclass

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
from yatagarasu_core.proofs import MarkerError as CoreMarkerError

_YGR1_RE = re.compile(r"(ygr1\.[A-Za-z0-9_-]+)")
_UNSCOPED_WORKSPACE = "<unscoped>"


@dataclass(slots=True)
class _PendingChain:
    input_event: SourceEventRef | None = None
    prompt_event: SourceEventRef | None = None
    decoded_marker: DeliveryMarker | None = None


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

        # (workspace_id, session_id) -> correlated proof chain. Session IDs are
        # not assumed to be globally unique across resident workspaces.
        self._active_chains: dict[
            tuple[str, str], tuple[Delivery, DeliveryMarker, list[SourceEventRef]]
        ] = {}

        # A resident observes every workspace on its host. Pending correlation
        # must therefore be workspace-scoped or interleaved turns overwrite
        # one another before the harness hook arrives.
        self._pending_chains: dict[str, _PendingChain] = {}

    def observe(
        self,
        event: SourceEventRef,
        payload: dict | None = None,
        *,
        observed_at: str,
        workspace_id: str | None = None,
    ) -> None:
        """Process an event from the cmux event bus."""
        name = event.event_name
        workspace_key = workspace_id or _UNSCOPED_WORKSPACE

        if name == "surface.input_sent":
            self._pending_chains[workspace_key] = _PendingChain(input_event=event)

        elif name == "workspace.prompt.submitted":
            pending = self._pending_chains.setdefault(workspace_key, _PendingChain())
            pending.prompt_event = event
            pending.decoded_marker = None
            if payload and "message_preview" in payload:
                match = _YGR1_RE.search(payload["message_preview"])
                if match:
                    with contextlib.suppress(CoreMarkerError):
                        pending.decoded_marker = MarkerAuthority.decode(match.group(1))
        elif name == "agent.hook.UserPromptSubmit":
            pending = self._pending_chains.pop(workspace_key, None)
            if not event.session_id:
                return

            if (
                pending
                and pending.input_event
                and pending.prompt_event
                and pending.decoded_marker
            ):
                context = self._delivery_lookup(pending.decoded_marker.delivery_id)
                if context:
                    delivery, core_marker = context
                    # The prompt correlation fields were produced from the
                    # observed DerivedEvent. Never replace them from the
                    # authoritative lookup or core compares a value with
                    # itself and the guard can no longer fail.
                    chain = [pending.input_event, pending.prompt_event, event]
                    self._active_chains[(workspace_key, event.session_id)] = (
                        delivery,
                        core_marker,
                        chain,
                    )

        elif name == "agent.hook.Stop":
            if not event.session_id:
                return

            chain_data = self._active_chains.pop(
                (workspace_key, event.session_id), None
            )
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

            if not delivery.binding_id:
                return

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
