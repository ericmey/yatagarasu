"""Reconnectable CMUX event-stream resident."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .event_outbox import CommitDisposition, DerivedEvent, EventCursor, EventOutbox
from .socket_client import UnixCmuxSocketClient
from .stream_protocol import EventProjector, StreamAck, StreamProtocolError


class ReceiptProducer(Protocol):
    """Consumes evidence before its cursor is made durable.

    ``observe`` returns only after any emitted receipt is durably accepted or
    durably queued. A transient sink failure raises, leaving the event
    replayable because the resident has not advanced its cursor yet.
    """

    def recover(self, events: tuple[DerivedEvent, ...]) -> None: ...

    def observe(self, event: DerivedEvent) -> None: ...


@dataclass(frozen=True, slots=True)
class ResidentRun:
    """Literal observations from a bounded resident run."""

    connections: int
    reconnect_after_seq: tuple[int | None, ...]
    slow_consumer_received: bool
    disconnect_at_seq: int | None
    replay_event_count: int
    inserted_event_count: int
    duplicate_event_count: int
    snapshot_commands: tuple[str, ...]


class EventStreamResident:
    """Consume CMUX evidence without ever owning or invoking pane injection."""

    def __init__(
        self,
        *,
        source_instance_id: str,
        client: UnixCmuxSocketClient,
        outbox: EventOutbox,
        marker_key: bytes,
        receipt_producer: ReceiptProducer | None = None,
    ) -> None:
        if not source_instance_id:
            raise ValueError("source instance ID must not be empty")
        self.source_instance_id = source_instance_id
        self.client = client
        self.outbox = outbox
        self.receipt_producer = receipt_producer
        self.projector = EventProjector(
            source_instance_id=source_instance_id, marker_key=marker_key
        )

    def run(self, *, max_connections: int = 1) -> ResidentRun:
        """Run a bounded reconnect loop suitable for a runtime supervisor."""
        if max_connections <= 0:
            raise ValueError("max_connections must be positive")
        reconnect_after: list[int | None] = []
        snapshot_commands: list[str] = []
        slow_consumer = False
        disconnect_at_seq: int | None = None
        replay_count = 0
        inserted = 0
        duplicates = 0

        if self.receipt_producer is not None:
            self.receipt_producer.recover(
                self.outbox.outbox_events(self.source_instance_id)
            )

        for _ in range(max_connections):
            durable_before = self.outbox.cursor(self.source_instance_id)
            after_seq = durable_before.seq if durable_before is not None else None
            reconnect_after.append(after_seq)
            with self.client.open_stream(after_seq=after_seq) as stream:
                first = stream.read_frame()
                if first is None:
                    raise StreamProtocolError("CMUX stream closed before ack")
                ack = StreamAck.parse(first)
                gap = ack.resume.gap or (
                    durable_before is not None and durable_before.boot_id != ack.boot_id
                )
                remaining_replay = ack.replay_count
                replay_seen = 0
                inserted_before = inserted
                duplicates_before = duplicates

                if gap and remaining_replay == 0:
                    commands = self._resnapshot(
                        ack,
                        cursor_seq=ack.resume.latest_seq,
                        replay_count=0,
                    )
                    snapshot_commands.extend(commands)

                while True:
                    frame = stream.read_frame()
                    if frame is None:
                        break
                    frame_type = frame.get("type")
                    if frame_type == "heartbeat":
                        continue
                    if frame_type == "error":
                        error = frame.get("error")
                        code = error.get("code") if isinstance(error, dict) else None
                        if code != "slow_consumer":
                            raise StreamProtocolError(f"CMUX stream error: {code}")
                        slow_consumer = True
                        latest = error.get("latest_seq")
                        if (
                            not isinstance(latest, int)
                            or isinstance(latest, bool)
                            or latest < 0
                        ):
                            raise StreamProtocolError(
                                "slow_consumer latest sequence is malformed"
                            )
                        disconnect_at_seq = latest
                        break
                    if frame_type != "event":
                        raise StreamProtocolError("unexpected CMUX stream frame type")

                    event = self.projector.project(frame, expected_boot_id=ack.boot_id)
                    # Feed the receipt plane before advancing the durable stream
                    # cursor. If the sink fails, this event remains replayable.
                    # A receipt uses the source event ID, so replay after a crash
                    # is classified by core as duplicate rather than a new claim.
                    if self.receipt_producer is not None:
                        self.receipt_producer.observe(event)
                    disposition = self.outbox.commit_event(
                        event, permit_boot_change=gap
                    )
                    if disposition is CommitDisposition.INSERTED:
                        inserted += 1
                    else:
                        duplicates += 1

                    if remaining_replay > 0:
                        remaining_replay -= 1
                        replay_seen += 1
                        replay_count += 1
                        if remaining_replay == 0 and gap:
                            commands = self._resnapshot(
                                ack,
                                cursor_seq=event.seq,
                                replay_count=replay_seen,
                            )
                            snapshot_commands.extend(commands)

                if remaining_replay != 0:
                    raise StreamProtocolError(
                        "stream ended before declared replay completed"
                    )
                if durable_before is not None:
                    durable_after = self.outbox.cursor(self.source_instance_id)
                    if durable_after is None:
                        raise StreamProtocolError("reconnect lost the durable cursor")
                    self.outbox.record_reconnect_replay(
                        durable_after,
                        requested_after_seq=after_seq,
                        replay_count=replay_seen,
                        inserted_count=inserted - inserted_before,
                        duplicate_count=duplicates - duplicates_before,
                        disconnect_at_seq=disconnect_at_seq,
                    )

        return ResidentRun(
            connections=max_connections,
            reconnect_after_seq=tuple(reconnect_after),
            slow_consumer_received=slow_consumer,
            disconnect_at_seq=disconnect_at_seq,
            replay_event_count=replay_count,
            inserted_event_count=inserted,
            duplicate_event_count=duplicates,
            snapshot_commands=tuple(snapshot_commands),
        )

    def _resnapshot(
        self, ack: StreamAck, *, cursor_seq: int, replay_count: int
    ) -> tuple[str, ...]:
        commands = self.client.snapshots()
        self.outbox.record_resnapshot(
            EventCursor(self.source_instance_id, ack.boot_id, cursor_seq),
            snapshots=commands,
            replay_count=replay_count,
        )
        return tuple(snapshot.method for snapshot in commands)
