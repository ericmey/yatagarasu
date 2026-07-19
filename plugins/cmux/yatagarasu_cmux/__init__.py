"""Session-transport plugin for cmux-hosted agent sessions.

Delivers a message into a live session and proves — or disproves — that it
landed. Declares ``session-bound`` only: it never emits participant evidence and
never synthesizes session entry it cannot observe.
"""

from .event_outbox import (
    CommitDisposition,
    DerivedEvent,
    EventCursor,
    EventOutbox,
    OutboxError,
    SnapshotBaseline,
    default_event_outbox_path,
)
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
from .resident import EventStreamResident, ResidentRun
from .socket_client import SNAPSHOT_METHODS, UnixCmuxSocketClient
from .stream_protocol import EventProjector, StreamAck, StreamProtocolError

__all__ = [
    "EVENT_INPUT_SENT",
    "EVENT_PROMPT_SUBMITTED",
    "REQUIRED_EVENTS",
    "SNAPSHOT_METHODS",
    "BusObserver",
    "CommitDisposition",
    "DerivedEvent",
    "EventCursor",
    "EventOutbox",
    "EventProjector",
    "EventStreamResident",
    "Injector",
    "Marker",
    "MarkerError",
    "OutboxError",
    "ResidentRun",
    "ResolutionError",
    "Resolver",
    "SnapshotBaseline",
    "StreamAck",
    "StreamProtocolError",
    "SubmitOutcome",
    "SubmitResult",
    "Transport",
    "UnixCmuxSocketClient",
    "default_event_outbox_path",
    "extract",
    "mint",
    "redact",
]
