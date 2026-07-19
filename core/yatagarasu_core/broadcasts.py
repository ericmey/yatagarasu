"""Atomic room broadcast expansion and durable per-seat outcomes."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from uuid import uuid4

from .proofs import AUTHORITY_SCOPE, parse_timestamp
from .store import CoreStore
from .types import (
    BindingState,
    BroadcastOutcome,
    BroadcastResult,
    DeliveryMode,
    DeliveryState,
    Disposition,
)

IdFactory = Callable[[str], str]


class BroadcastConflictError(RuntimeError):
    """The atomic broadcast expansion collided or became inconsistent."""


def _uuid_id(kind: str) -> str:
    return f"{kind}-{uuid4()}"


class BroadcastKernel:
    """Create one event, freeze one roster, and expand one delivery per seat."""

    def __init__(self, store: CoreStore, id_factory: IdFactory = _uuid_id) -> None:
        self.store = store
        self._id_factory = id_factory

    def broadcast(
        self,
        *,
        actor_id: str,
        room_id: str,
        content: str,
        accepted_at: str,
    ) -> BroadcastResult:
        if not actor_id or not room_id or not content:
            raise ValueError("actor, room, and content must not be empty")
        parse_timestamp(accepted_at)
        broadcast_id = self._id_factory("broadcast")
        event_id = self._id_factory("event")
        try:
            with self.store.connection:
                self.store.connection.execute("BEGIN IMMEDIATE")
                roster = tuple(
                    recipient
                    for recipient in self.store.room_roster(room_id)
                    if recipient != actor_id
                )
                if not roster:
                    raise ValueError("broadcast roster is empty")
                rows: list[tuple[str, str, str | None]] = []
                for recipient_id in roster:
                    delivery_id = self._id_factory("delivery")
                    attempt_id = self._id_factory("attempt")
                    binding = self.store.active_session_binding_for(
                        recipient_id, observed_at=accepted_at
                    )
                    rows.append(
                        (
                            delivery_id,
                            attempt_id,
                            binding["binding_id"] if binding is not None else None,
                        )
                    )
                if len({delivery_id for delivery_id, _, _ in rows}) != len(rows):
                    raise BroadcastConflictError("delivery ID collision during fan-out")
                self.store.connection.execute(
                    """INSERT INTO canonical_events
                       (event_id, actor_id, room_id, content, accepted_at,
                        authority_scope) VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        event_id,
                        actor_id,
                        room_id,
                        content,
                        accepted_at,
                        AUTHORITY_SCOPE,
                    ),
                )
                self.store.connection.execute(
                    """INSERT INTO broadcasts
                       (broadcast_id, event_id, room_id, roster_snapshot_size)
                       VALUES (?, ?, ?, ?)""",
                    (broadcast_id, event_id, room_id, len(roster)),
                )
                for ordinal, (recipient_id, row) in enumerate(
                    zip(roster, rows, strict=True)
                ):
                    delivery_id, attempt_id, binding_id = row
                    self.store.connection.execute(
                        """INSERT INTO deliveries
                           (delivery_id, event_id, attempt_id, binding_id,
                            recipient_id, delivery_mode, state, disposition)
                           VALUES (?, ?, ?, ?, ?, ?, ?, NULL)""",
                        (
                            delivery_id,
                            event_id,
                            attempt_id,
                            binding_id,
                            recipient_id,
                            DeliveryMode.SESSION_BOUND.value,
                            DeliveryState.QUEUED.value,
                        ),
                    )
                    self.store.connection.execute(
                        """INSERT INTO broadcast_recipients
                           (broadcast_id, recipient_id, roster_ordinal, binding_id,
                            delivery_id) VALUES (?, ?, ?, ?, ?)""",
                        (
                            broadcast_id,
                            recipient_id,
                            ordinal,
                            binding_id,
                            delivery_id,
                        ),
                    )
                self.store.connection.execute(
                    """INSERT INTO broadcast_audit
                       (broadcast_id, event_id, roster_snapshot_size)
                       VALUES (?, ?, ?)""",
                    (broadcast_id, event_id, len(roster)),
                )
        except sqlite3.IntegrityError as exc:
            raise BroadcastConflictError("broadcast expansion was rolled back") from exc

        return self.result(broadcast_id)

    def result(self, broadcast_id: str) -> BroadcastResult:
        header = self.store.connection.execute(
            "SELECT * FROM broadcasts WHERE broadcast_id = ?", (broadcast_id,)
        ).fetchone()
        if header is None:
            raise KeyError(broadcast_id)
        rows = self.store.connection.execute(
            """SELECT br.recipient_id, br.delivery_id, br.binding_id,
                      br.roster_ordinal, d.state, d.disposition, sb.state AS binding_state
               FROM broadcast_recipients br
               JOIN deliveries d ON d.delivery_id = br.delivery_id
               LEFT JOIN session_bindings sb ON sb.binding_id = br.binding_id
               WHERE br.broadcast_id = ? ORDER BY br.roster_ordinal""",
            (broadcast_id,),
        ).fetchall()
        audit = self.store.broadcast_audit(broadcast_id)
        if (
            len(rows) != header["roster_snapshot_size"]
            or audit is None
            or audit["event_id"] != header["event_id"]
            or audit["roster_snapshot_size"] != header["roster_snapshot_size"]
        ):
            raise BroadcastConflictError("broadcast snapshot is inconsistent")
        outcomes = tuple(self._outcome(row) for row in rows)
        delivered_states = {
            DeliveryState.TRANSPORT_SUBMITTED,
            DeliveryState.IN_SESSION,
            DeliveryState.PROCESSED,
        }
        return BroadcastResult(
            broadcast_id=broadcast_id,
            event_id=header["event_id"],
            room_id=header["room_id"],
            roster_snapshot_size=header["roster_snapshot_size"],
            outcomes=outcomes,
            all_delivered=bool(outcomes)
            and all(outcome.state in delivered_states for outcome in outcomes),
        )

    @staticmethod
    def _outcome(row: sqlite3.Row) -> BroadcastOutcome:
        delivery_state = DeliveryState(row["state"])
        binding_state = row["binding_state"]
        binding_is_load_bearing = delivery_state in {
            DeliveryState.QUEUED,
            DeliveryState.DISPATCHING,
        }
        if binding_is_load_bearing and row["binding_id"] is None:
            unavailable_reason = "binding-absent"
        elif binding_is_load_bearing and binding_state != BindingState.ACTIVE.value:
            unavailable_reason = "binding-revoked-or-superseded"
        else:
            unavailable_reason = None
        return BroadcastOutcome(
            recipient_id=row["recipient_id"],
            delivery_id=row["delivery_id"],
            binding_id=row["binding_id"],
            state=delivery_state,
            disposition=(
                Disposition(row["disposition"]) if row["disposition"] else None
            ),
            unavailable_reason=unavailable_reason,
        )
