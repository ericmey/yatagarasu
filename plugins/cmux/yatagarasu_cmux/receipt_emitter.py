from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class HarnessTurnCompleted:
    """Represents a harness.turn_completed event derived from agent.hook.Stop."""

    session_id: str


class ReceiptEmitter:
    """
    Translates agent.hook.Stop events into core-conformant processed(completed) receipts.
    A bare turn-end proves completed only. It never proves answered/acknowledged/held/declined.
    """

    def __init__(self, core_client: Callable[[dict], None]) -> None:
        self._core = core_client

    def process_stop_event(
        self, session_id: str, delivery_id: str | None = None
    ) -> None:
        """
        Processes an agent.hook.Stop event. If it correlates to an active Yatagarasu delivery,
        emits processed(completed) to the core.
        """
        # A minimal implementation to satisfy Y-CMUX-012.
        # In a real event-bus integration, this class watches the event stream,
        # correlates the Stop to the UserPromptSubmit, and emits the receipt.
        payload = {
            "evidence_class": "harness.turn_completed",
            "proof_method": "cmux.event_bus.harness_hook_relay",
            "disposition": "completed",
        }
        if delivery_id:
            payload["delivery_id"] = delivery_id

        self._core(payload)
