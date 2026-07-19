"""Strict CMUX event-frame parsing and privacy-preserving projection."""

from __future__ import annotations

import json
from dataclasses import dataclass

from .event_outbox import DerivedEvent
from .marker import extract, marker_text

PROTOCOL = "cmux-events"
PROTOCOL_VERSION = 1
MAX_FRAME_BYTES = 16 * 1024

_SAFE_PAYLOAD_KEYS = frozenset(
    {
        "_opencode_request_id",
        "_source",
        "decision",
        "hook_event_name",
        "message_length",
        "payload_truncated",
        "phase",
        "redacted_fields",
        "request_id",
        "session_id",
        "status",
        "tool_name",
        "workspace_id",
    }
)


def _is_safe_payload_value(value: object) -> bool:
    scalar_types = (str, int, float, bool, type(None))
    if isinstance(value, scalar_types):
        return True
    return isinstance(value, list) and all(
        isinstance(item, scalar_types) for item in value
    )


class StreamProtocolError(RuntimeError):
    """A socket frame violates the documented CMUX events protocol."""


@dataclass(frozen=True, slots=True)
class ResumeMetadata:
    """Literal resume evidence carried by the stream acknowledgement."""

    requested_after_seq: int | None
    oldest_seq: int
    latest_seq: int
    next_seq: int
    gap: bool


@dataclass(frozen=True, slots=True)
class StreamAck:
    """Validated first frame for one event-stream connection."""

    boot_id: str
    replay_count: int
    resume: ResumeMetadata

    @classmethod
    def parse(cls, frame: dict[str, object]) -> StreamAck:
        if (
            frame.get("type") != "ack"
            or frame.get("protocol") != PROTOCOL
            or frame.get("version") != PROTOCOL_VERSION
        ):
            raise StreamProtocolError("first frame is not a supported stream ack")
        boot_id = frame.get("boot_id")
        replay_count = frame.get("replay_count")
        resume = frame.get("resume")
        if (
            not isinstance(boot_id, str)
            or not boot_id
            or not isinstance(replay_count, int)
            or isinstance(replay_count, bool)
            or replay_count < 0
            or not isinstance(resume, dict)
        ):
            raise StreamProtocolError("stream ack identity is malformed")
        requested = resume.get("requested_after_seq")
        oldest = resume.get("oldest_seq")
        latest = resume.get("latest_seq")
        next_seq = resume.get("next_seq")
        gap = resume.get("gap")
        if requested is not None and (
            not isinstance(requested, int) or isinstance(requested, bool)
        ):
            raise StreamProtocolError("resume requested cursor is malformed")
        if (
            not isinstance(oldest, int)
            or isinstance(oldest, bool)
            or not isinstance(latest, int)
            or isinstance(latest, bool)
            or not isinstance(next_seq, int)
            or isinstance(next_seq, bool)
            or not isinstance(gap, bool)
            or min(oldest, latest, next_seq) < 0
        ):
            raise StreamProtocolError("resume metadata is malformed")
        return cls(
            boot_id,
            replay_count,
            ResumeMetadata(requested, oldest, latest, next_seq, gap),
        )


class EventProjector:
    """Turn a local-sensitive bus event into content-free durable evidence."""

    def __init__(self, *, source_instance_id: str, marker_key: bytes) -> None:
        if not source_instance_id:
            raise ValueError("source instance ID must not be empty")
        self.source_instance_id = source_instance_id
        self.marker_key = marker_key

    def project(
        self, frame: dict[str, object], *, expected_boot_id: str
    ) -> DerivedEvent:
        if (
            frame.get("type") != "event"
            or frame.get("protocol") != PROTOCOL
            or frame.get("version") != PROTOCOL_VERSION
        ):
            raise StreamProtocolError("frame is not a supported CMUX event")
        boot_id = frame.get("boot_id")
        seq = frame.get("seq")
        event_id = frame.get("id")
        name = frame.get("name")
        if boot_id != expected_boot_id:
            raise StreamProtocolError("event boot ID differs from stream ack")
        if (
            not isinstance(boot_id, str)
            or not isinstance(seq, int)
            or isinstance(seq, bool)
            or seq < 0
            or not isinstance(event_id, str)
            or not event_id
            or not isinstance(name, str)
            or not name
        ):
            raise StreamProtocolError("event identity is malformed")

        payload = frame.get("payload")
        safe_payload: dict[str, object] = {}
        if isinstance(payload, dict):
            safe_payload = {
                key: value
                for key, value in payload.items()
                if key in _SAFE_PAYLOAD_KEYS and _is_safe_payload_value(value)
            }
            preview = payload.get("message_preview")
            if isinstance(preview, str):
                marker = extract(None, preview)
                if marker is not None:
                    # Persist only the marker token; surrounding prompt content
                    # never crosses the host-local projection boundary.
                    safe_payload["message_preview"] = marker_text(marker)

        projected = {
            "boot_id": boot_id,
            "category": frame.get("category"),
            "event_id": event_id,
            "name": name,
            "occurred_at": frame.get("occurred_at"),
            "pane_id": frame.get("pane_id"),
            "payload": safe_payload,
            "seq": seq,
            "source": frame.get("source"),
            "surface_id": frame.get("surface_id"),
            "window_id": frame.get("window_id"),
            "workspace_id": frame.get("workspace_id"),
        }
        return DerivedEvent(
            source_instance_id=self.source_instance_id,
            boot_id=boot_id,
            seq=seq,
            source_event_id=event_id,
            event_name=name,
            event_json=json.dumps(projected, separators=(",", ":"), sort_keys=True),
        )
