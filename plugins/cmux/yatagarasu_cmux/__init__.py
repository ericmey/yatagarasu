"""Session-transport plugin for cmux-hosted agent sessions.

Delivers a message into a live session and proves — or disproves — that it
landed. Declares ``session-bound`` only: it never emits participant evidence and
never synthesizes session entry it cannot observe.
"""

from .injector import (
    EVENT_INPUT_SENT,
    EVENT_PROMPT_SUBMITTED,
    REQUIRED_EVENTS,
    BusObserver,
    Injector,
    ResolutionError,
    Resolver,
    Transport,
)
from .marker import Marker, MarkerError, extract, mint, redact
from .outcome import SubmitOutcome, SubmitResult

__all__ = [
    "EVENT_INPUT_SENT",
    "EVENT_PROMPT_SUBMITTED",
    "REQUIRED_EVENTS",
    "BusObserver",
    "Injector",
    "Marker",
    "MarkerError",
    "ResolutionError",
    "Resolver",
    "SubmitOutcome",
    "SubmitResult",
    "Transport",
    "extract",
    "mint",
    "redact",
]
