"""Submit outcomes.

The whole point of this module is that there are **three** outcomes, not two.

The predecessor to this plugin scraped the target's composer to decide whether a
message had been submitted, and treated a failed scrape as success. That leniency
silently lost messages: the send reported fine and nothing arrived. The fix is not
a better scrape — it is refusing to collapse "I could not tell" into either
"delivered" or "failed".

``UNKNOWN`` is therefore a first-class outcome that must be surfaced and held, not
retried blindly and not rounded to a neighbour.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum


class SubmitOutcome(StrEnum):
    """Result of attempting to place a message into a session."""

    SUBMITTED = "submitted"
    """Both required host events observed. Positive proof."""

    NOT_SUBMITTED = "not_submitted"
    """The submit event never arrived within the window. A *true negative* —
    the message genuinely did not land, and the delivery may be re-queued."""

    UNKNOWN = "unknown"
    """The effect may or may not have fired and we cannot tell. Hold and surface.
    Never re-inject on this outcome: re-injection risks a duplicate turn, and the
    local side effect cannot be undone by retrying."""


@dataclass(frozen=True, slots=True)
class SubmitResult:
    """Outcome plus the evidence that produced it.

    ``source_events`` is the ordered chain of host event names observed. It is
    kept because a receipt is only as good as the evidence chain behind it, and a
    downstream reducer is expected to assert on the assembled bundle rather than
    on any single field.

    No message content is stored here, by construction.
    """

    outcome: SubmitOutcome
    delivery_id: str
    source_events: Sequence[str] = field(default_factory=tuple)
    detail: str = ""

    @property
    def is_proven(self) -> bool:
        return self.outcome is SubmitOutcome.SUBMITTED

    @property
    def may_requeue(self) -> bool:
        """Only a proven negative is safe to re-queue."""
        return self.outcome is SubmitOutcome.NOT_SUBMITTED

    @property
    def must_hold(self) -> bool:
        """Ambiguity is held for reconciliation, never retried."""
        return self.outcome is SubmitOutcome.UNKNOWN

    def __str__(self) -> str:  # pragma: no cover - trivial
        chain = " -> ".join(self.source_events) if self.source_events else "none"
        return (
            f"{self.outcome.value} delivery={self.delivery_id} "
            f"chain=[{chain}]{' ' + self.detail if self.detail else ''}"
        )
