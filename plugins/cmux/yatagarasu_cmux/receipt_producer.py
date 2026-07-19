"""Translate durable CMUX observations and feed the receipt emitter.

``EventProjector`` produces content-minimized ``DerivedEvent`` rows. Core's
proof validator consumes ``SourceEventRef`` objects. This module is the missing
producer seam between those types and deliberately sources correlation fields
from the observed wire marker, never from the authoritative delivery lookup.
"""

from __future__ import annotations

import json
from collections.abc import Callable

from yatagarasu_core import Delivery, DeliveryMarker, Receipt, SourceEventRef

from .event_outbox import DerivedEvent
from .marker import extract
from .receipt_emitter import ReceiptEmitter


class ReceiptProducerError(RuntimeError):
    """A durable event cannot be represented honestly as receipt evidence."""


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

    def recover(self, events: tuple[DerivedEvent, ...]) -> None:
        """Rebuild ephemeral chain state from the durable source outbox.

        Completed historical chains may re-emit their stable receipt IDs. The
        core reducer classifies those as duplicates; replaying is safer than
        losing a chain that was between ``UserPromptSubmit`` and ``Stop`` when
        the resident restarted.
        """
        self._emitter = self._new_emitter()
        for event in events:
            self.observe(event)

    def observe(self, event: DerivedEvent) -> None:
        """Translate one projected event and feed the receipt state machine."""
        source_event, payload, observed_at = source_event_from_derived(event)
        self._emitter.observe(
            source_event,
            payload=payload,
            observed_at=observed_at,
        )

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
        marker = extract(None, preview if isinstance(preview, str) else None)
        if marker is not None:
            binding_id = marker.binding_id
            marker_signature = marker.signature

    return (
        SourceEventRef(
            source_instance_id=event.source_instance_id,
            boot_id=event.boot_id,
            seq=event.seq,
            source_event_id=event.source_event_id,
            event_name=event.event_name,
            session_id=session_id,
            binding_id=binding_id,
            marker_signature=marker_signature,
        ),
        payload,
        observed_at,
    )
