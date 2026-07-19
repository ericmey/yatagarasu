"""Translate durable CMUX observations and feed the receipt emitter.

``EventProjector`` produces content-minimized ``DerivedEvent`` rows. Core's
proof validator consumes ``SourceEventRef`` objects. This module is the missing
producer seam between those types and deliberately sources correlation fields
from the observed wire marker, never from the authoritative delivery lookup.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field

from yatagarasu_core import Delivery, DeliveryMarker, Receipt, SourceEventRef

from .event_outbox import DerivedEvent
from .marker import extract
from .receipt_emitter import ReceiptEmitter


class ReceiptProducerError(RuntimeError):
    """A durable event cannot be represented honestly as receipt evidence."""


@dataclass(frozen=True, slots=True)
class _TranslatedObservation:
    source_event: SourceEventRef
    payload: dict[str, object]
    observed_at: str
    workspace_id: str | None


@dataclass(slots=True)
class _WorkspaceChain:
    """Names already accepted for one marker-bearing workspace turn."""

    marker_signature: str | None = None
    accepted: set[tuple[str, str | None]] = field(default_factory=set)


class DerivedEventReceiptProducer:
    """Own the production ``DerivedEvent`` -> ``ReceiptEmitter`` call path.

    ``core_client`` is an acknowledgement boundary: it must return only after
    the receipt is durably accepted or durably queued, and raise when it cannot
    do either. The resident deliberately calls this producer before advancing
    its stream cursor.
    """

    def __init__(
        self,
        *,
        core_client: Callable[[Receipt], None],
        provider_id: str,
        delivery_lookup: Callable[[str], tuple[Delivery, DeliveryMarker] | None],
    ) -> None:
        self._core_client = core_client
        self._provider_id = provider_id
        self._delivery_lookup = delivery_lookup
        self._emitter = self._new_emitter()
        self._workspace_chains: dict[str, _WorkspaceChain] = {}

    def recover(self, events: tuple[DerivedEvent, ...]) -> None:
        """Rebuild ephemeral chain state from the durable source outbox.

        Completed historical chains may re-emit their stable receipt IDs. The
        core reducer classifies those as duplicates; replaying is safer than
        losing a chain that was between ``UserPromptSubmit`` and ``Stop`` when
        the resident restarted.
        """
        self._emitter = self._new_emitter()
        self._workspace_chains.clear()
        for event in events:
            self.observe(event)

    def observe(self, event: DerivedEvent) -> None:
        """Translate one projected event and feed the receipt state machine."""
        translated = _translate_derived_event(event)
        if not self._accept_for_emission(translated):
            return
        self._emitter.observe(
            translated.source_event,
            payload=translated.payload,
            observed_at=translated.observed_at,
            workspace_id=translated.workspace_id,
        )

    def _accept_for_emission(self, event: _TranslatedObservation) -> bool:
        """Collapse duplicate callback pairs inside one workspace chain.

        CMUX may relay repeated prompt and UserPromptSubmit observations for a
        single marker. Durable source rows remain untouched; only the emitter
        input is normalized. The first observed event wins, preserving the
        exact source-event chain used by core proof validation.
        """
        workspace_key = event.workspace_id or "<unscoped>"
        name = event.source_event.event_name
        phase = event.payload.get("phase")

        # CMUX relays both received and completed hook phases. Only completed is
        # durable harness evidence. Older providers omitted phase; retain that
        # compatibility rather than silently dropping their single callback.
        if (
            name.startswith("agent.hook.")
            and isinstance(phase, str)
            and phase != "completed"
        ):
            return False

        if name == "surface.input_sent":
            self._workspace_chains[workspace_key] = _WorkspaceChain()
            return True

        chain = self._workspace_chains.setdefault(workspace_key, _WorkspaceChain())
        if name == "workspace.prompt.submitted":
            signature = event.source_event.marker_signature
            if chain.marker_signature not in (None, signature):
                chain.accepted.clear()
            chain.marker_signature = signature
        else:
            signature = chain.marker_signature

        key = (name, signature)
        if name in {
            "workspace.prompt.submitted",
            "agent.hook.UserPromptSubmit",
            "agent.hook.Stop",
        }:
            if key in chain.accepted:
                return False
            chain.accepted.add(key)

        return True

    def _new_emitter(self) -> ReceiptEmitter:
        return ReceiptEmitter(
            core_client=self._core_client,
            provider_id=self._provider_id,
            delivery_lookup=self._delivery_lookup,
        )


def source_event_from_derived(
    event: DerivedEvent,
) -> tuple[SourceEventRef, dict[str, object], str]:
    """Return core evidence using only values present in ``event``.

    The decoded marker is untrusted. Its binding and signature are copied into
    the source reference precisely so core can compare those wire claims with
    its independently held delivery marker.
    """
    translated = _translate_derived_event(event)
    return translated.source_event, translated.payload, translated.observed_at


def _translate_derived_event(event: DerivedEvent) -> _TranslatedObservation:
    try:
        projected = json.loads(event.event_json)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ReceiptProducerError("derived event JSON is malformed") from exc
    if not isinstance(projected, dict):
        raise ReceiptProducerError("derived event JSON is not an object")

    expected_identity = {
        "boot_id": event.boot_id,
        "event_id": event.source_event_id,
        "name": event.event_name,
        "seq": event.seq,
    }
    if any(projected.get(key) != value for key, value in expected_identity.items()):
        raise ReceiptProducerError("derived event identity contradicts its row")

    raw_payload = projected.get("payload")
    payload = raw_payload if isinstance(raw_payload, dict) else {}
    observed_at = projected.get("occurred_at")
    if not isinstance(observed_at, str) or not observed_at:
        raise ReceiptProducerError("derived event has no observation timestamp")

    session_id = payload.get("session_id")
    if not isinstance(session_id, str):
        session_id = None

    binding_id = None
    marker_signature = None
    if event.event_name == "workspace.prompt.submitted":
        preview = payload.get("message_preview")
        marker = extract(preview) if isinstance(preview, str) else None
        if marker is not None:
            binding_id = marker.binding_id
            marker_signature = marker.signature

    workspace_id = projected.get("workspace_id")
    if not isinstance(workspace_id, str) or not workspace_id:
        workspace_id = None

    return _TranslatedObservation(
        source_event=SourceEventRef(
            source_instance_id=event.source_instance_id,
            boot_id=event.boot_id,
            seq=event.seq,
            source_event_id=event.source_event_id,
            event_name=event.event_name,
            session_id=session_id,
            binding_id=binding_id,
            marker_signature=marker_signature,
        ),
        payload=payload,
        observed_at=observed_at,
        workspace_id=workspace_id,
    )
