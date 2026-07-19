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
from .harness_profiles import (
    BusyEnterBehavior,
    HarnessKind,
    HarnessProfile,
    profile_for,
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
from .marker import (
    MAX_SHORT_MARKER_CHARS,
    Marker,
    MarkerError,
    ShortMarker,
    encode_short,
    extract,
    mint,
    redact,
)
from .notifications import (
    IN_SESSION_EVIDENCE,
    NotificationLifecycle,
    NotificationLifecycleError,
    NotificationRecord,
    validate_hook_effects,
)
from .outcome import SubmitOutcome, SubmitResult
from .receipt_producer import (
    DerivedEventReceiptProducer,
    ReceiptProducerError,
    source_event_from_derived,
)
from .resident import EventStreamResident, ResidentRun
from .socket_client import SNAPSHOT_METHODS, UnixCmuxSocketClient
from .socket_transport import CmuxSocketTransport
from .stream_protocol import EventProjector, StreamAck, StreamProtocolError

__all__ = [
    "EVENT_INPUT_SENT",
    "EVENT_PROMPT_SUBMITTED",
    "IN_SESSION_EVIDENCE",
    "MAX_SHORT_MARKER_CHARS",
    "REQUIRED_EVENTS",
    "SNAPSHOT_METHODS",
    "BusObserver",
    "BusyEnterBehavior",
    "CmuxSocketTransport",
    "CommitDisposition",
    "DerivedEvent",
    "DerivedEventReceiptProducer",
    "EventCursor",
    "EventOutbox",
    "EventProjector",
    "EventStreamResident",
    "HarnessKind",
    "HarnessProfile",
    "Injector",
    "Marker",
    "MarkerError",
    "NotificationLifecycle",
    "NotificationLifecycleError",
    "NotificationRecord",
    "OutboxError",
    "ReceiptProducerError",
    "ResidentRun",
    "ResolutionError",
    "Resolver",
    "ShortMarker",
    "SnapshotBaseline",
    "StreamAck",
    "StreamProtocolError",
    "SubmitOutcome",
    "SubmitResult",
    "Transport",
    "UnixCmuxSocketClient",
    "default_event_outbox_path",
    "encode_short",
    "extract",
    "mint",
    "profile_for",
    "redact",
    "source_event_from_derived",
    "validate_hook_effects",
]
