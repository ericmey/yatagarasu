"""Durable, receipt-driven CMUX notification lifecycle."""

from __future__ import annotations

import hashlib
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .stream_protocol import StreamProtocolError

IN_SESSION_EVIDENCE = frozenset({"harness.prompt_accepted", "harness.turn_started"})


class NotificationLifecycleError(RuntimeError):
    """A banner operation could not reach an evidenced outcome."""


class CommandClient(Protocol):
    def call(self, method: str, params: dict[str, object] | None = None) -> object: ...


@dataclass(frozen=True, slots=True)
class NotificationRecord:
    event_id: str
    delivery_id: str
    seat_id: str
    notification_id: str
    workspace_id: str
    surface_id: str
    correlation_token: str
    created_at: float
    expires_at: float


class NotificationLifecycle:
    """Create banners once and retire them only on exact lifecycle evidence.

    The prepared row is committed before calling CMUX. If the process dies after
    CMUX creates the banner but before the notification id is recorded, retry
    reconciles by the unique correlation token rather than creating a duplicate.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        client: CommandClient,
        suppress_only_focused_surface: bool,
        per_seat_cap: int = 8,
        mailbox_ttl_s: float = 86_400,
        clock=time.time,
    ) -> None:
        if not suppress_only_focused_surface:
            raise NotificationLifecycleError(
                "notifications.suppressOnlyFocusedSurface must be true"
            )
        if per_seat_cap < 1 or mailbox_ttl_s <= 0:
            raise ValueError("notification cap and TTL must be positive")
        self.client = client
        self.per_seat_cap = per_seat_cap
        self.mailbox_ttl_s = mailbox_ttl_s
        self.clock = clock
        database_path = Path(path)
        database_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(database_path, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.execute(
            """CREATE TABLE IF NOT EXISTS notification_lifecycle (
                   event_id TEXT NOT NULL,
                   delivery_id TEXT PRIMARY KEY,
                   seat_id TEXT NOT NULL,
                   notification_id TEXT UNIQUE,
                   workspace_id TEXT NOT NULL,
                   surface_id TEXT NOT NULL,
                   correlation_token TEXT NOT NULL UNIQUE,
                   title TEXT NOT NULL,
                   subtitle TEXT NOT NULL,
                   body TEXT NOT NULL,
                   state TEXT NOT NULL CHECK (state IN
                       ('prepared', 'active', 'dismissed', 'expired', 'evicted')),
                   created_at REAL NOT NULL,
                   expires_at REAL NOT NULL,
                   retired_reason TEXT
               )"""
        )
        self._db.execute(
            """CREATE INDEX IF NOT EXISTS notification_lifecycle_event
               ON notification_lifecycle(event_id)"""
        )

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> NotificationLifecycle:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def publish(
        self,
        *,
        event_id: str,
        delivery_id: str,
        seat_id: str,
        workspace_id: str,
        surface_id: str,
        title: str,
        body: str,
        subtitle: str = "",
    ) -> NotificationRecord:
        now = self.clock()
        self.expire(now=now)
        token = self._token(event_id, delivery_id)
        display_subtitle = f"{subtitle} · {token}" if subtitle else token
        existing = self._row_for_delivery(delivery_id)
        if existing is not None:
            self._assert_same_claim(
                existing,
                event_id=event_id,
                delivery_id=delivery_id,
                seat_id=seat_id,
                workspace_id=workspace_id,
                surface_id=surface_id,
                title=title,
                subtitle=display_subtitle,
                body=body,
            )
            if existing["state"] == "active":
                return self._record(existing)
            if existing["state"] != "prepared":
                raise NotificationLifecycleError(
                    f"event already retired: {existing['state']}"
                )
        else:
            try:
                self._db.execute(
                    """INSERT INTO notification_lifecycle
                       (event_id, delivery_id, seat_id, notification_id,
                        workspace_id, surface_id, correlation_token, title,
                        subtitle, body, state, created_at, expires_at)
                       VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, 'prepared', ?, ?)""",
                    (
                        event_id,
                        delivery_id,
                        seat_id,
                        workspace_id,
                        surface_id,
                        token,
                        title,
                        display_subtitle,
                        body,
                        now,
                        now + self.mailbox_ttl_s,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                winner = self._row_for_delivery(delivery_id)
                if winner is None:
                    raise NotificationLifecycleError(
                        "notification_claim_conflict"
                    ) from exc
                self._assert_same_claim(
                    winner,
                    event_id=event_id,
                    delivery_id=delivery_id,
                    seat_id=seat_id,
                    workspace_id=workspace_id,
                    surface_id=surface_id,
                    title=title,
                    subtitle=display_subtitle,
                    body=body,
                )
            existing = self._row_for_delivery(delivery_id)
            assert existing is not None

        matches = self._matching_notifications(existing)
        if not matches:
            self.client.call(
                "notification.create_for_target",
                {
                    "workspace_id": workspace_id,
                    "surface_id": surface_id,
                    "title": existing["title"],
                    "subtitle": existing["subtitle"],
                    "body": existing["body"],
                },
            )
            matches = self._matching_notifications(existing)
        if len(matches) != 1:
            raise NotificationLifecycleError(
                f"notification_correlation_ambiguous:{len(matches)}"
            )
        notification_id = matches[0].get("id")
        if not isinstance(notification_id, str) or not notification_id:
            raise NotificationLifecycleError("notification_id_missing")
        self._db.execute(
            """UPDATE notification_lifecycle
               SET notification_id = ?, state = 'active'
               WHERE delivery_id = ? AND state = 'prepared'""",
            (notification_id, delivery_id),
        )
        self._enforce_cap(seat_id, keep_delivery_id=delivery_id)
        active = self._row_for_delivery(delivery_id)
        assert active is not None
        return self._record(active)

    def on_receipt(
        self,
        *,
        event_id: str,
        delivery_id: str,
        status: str,
        state: str,
        evidence_class: str,
    ) -> bool:
        """Dismiss only an accepted receipt that proves authoritative entry."""
        if (
            status != "accepted"
            or state != "in-session"
            or evidence_class not in IN_SESSION_EVIDENCE
        ):
            return False
        row = self._row_for_delivery(delivery_id)
        if row is None or row["event_id"] != event_id:
            return False
        return self._retire_delivery(delivery_id, "in-session-receipt")

    def on_notification_read(self, notification_id: str) -> bool:
        """Treat an exact CMUX mark-read event as explicit user acknowledgement."""
        row = self._db.execute(
            """SELECT delivery_id FROM notification_lifecycle
               WHERE notification_id = ? AND state = 'active'""",
            (notification_id,),
        ).fetchone()
        return bool(
            row and self._retire_delivery(row["delivery_id"], "explicit-mark-read")
        )

    def expire(self, *, now: float | None = None) -> int:
        cutoff = self.clock() if now is None else now
        rows = self._db.execute(
            """SELECT delivery_id FROM notification_lifecycle
               WHERE state IN ('prepared', 'active') AND expires_at <= ?
               ORDER BY created_at, event_id""",
            (cutoff,),
        ).fetchall()
        for row in rows:
            self._retire_delivery(row["delivery_id"], "expired", state="expired")
        return len(rows)

    def active_records(
        self, seat_id: str | None = None
    ) -> tuple[NotificationRecord, ...]:
        query = "SELECT * FROM notification_lifecycle WHERE state = 'active'"
        params: tuple[object, ...] = ()
        if seat_id is not None:
            query += " AND seat_id = ?"
            params = (seat_id,)
        query += " ORDER BY created_at, event_id"
        return tuple(self._record(row) for row in self._db.execute(query, params))

    def _retire_delivery(
        self, delivery_id: str, reason: str, *, state: str = "dismissed"
    ) -> bool:
        row = self._row_for_delivery(delivery_id)
        if row is None or row["state"] not in {"prepared", "active"}:
            return False
        notification_ids = []
        if row["notification_id"]:
            notification_ids.append(row["notification_id"])
        else:
            notification_ids.extend(
                item["id"]
                for item in self._matching_notifications(row)
                if isinstance(item.get("id"), str)
            )
        for notification_id in notification_ids:
            try:
                self.client.call("notification.dismiss", {"id": notification_id})
            except StreamProtocolError:
                if self._notification_exists(notification_id):
                    raise
        self._db.execute(
            """UPDATE notification_lifecycle
               SET state = ?, retired_reason = ? WHERE delivery_id = ?""",
            (state, reason, delivery_id),
        )
        return True

    def _enforce_cap(self, seat_id: str, *, keep_delivery_id: str) -> None:
        rows = self._db.execute(
            """SELECT event_id, delivery_id FROM notification_lifecycle
               WHERE seat_id = ? AND state = 'active'
               ORDER BY created_at, event_id""",
            (seat_id,),
        ).fetchall()
        excess = max(0, len(rows) - self.per_seat_cap)
        evictable = [row for row in rows if row["delivery_id"] != keep_delivery_id]
        for row in evictable[:excess]:
            self._retire_delivery(row["delivery_id"], "per-seat-cap", state="evicted")

    def _matching_notifications(self, row: sqlite3.Row) -> list[dict[str, object]]:
        result = self.client.call("notification.list", {})
        if not isinstance(result, dict):
            raise NotificationLifecycleError("notification_list_shape_invalid")
        items = result.get("notifications")
        if not isinstance(items, list):
            raise NotificationLifecycleError("notification_list_shape_invalid")
        return [
            item
            for item in items
            if isinstance(item, dict)
            and item.get("workspace_id") == row["workspace_id"]
            and item.get("surface_id") == row["surface_id"]
            and item.get("subtitle") == row["subtitle"]
        ]

    def _notification_exists(self, notification_id: str) -> bool:
        result = self.client.call("notification.list", {})
        return bool(
            isinstance(result, dict)
            and isinstance(result.get("notifications"), list)
            and any(
                isinstance(item, dict) and item.get("id") == notification_id
                for item in result["notifications"]
            )
        )

    def _row_for_delivery(self, delivery_id: str) -> sqlite3.Row | None:
        return self._db.execute(
            "SELECT * FROM notification_lifecycle WHERE delivery_id = ?",
            (delivery_id,),
        ).fetchone()

    @staticmethod
    def _assert_same_claim(
        row: sqlite3.Row,
        *,
        event_id: str,
        delivery_id: str,
        seat_id: str,
        workspace_id: str,
        surface_id: str,
        title: str,
        subtitle: str,
        body: str,
    ) -> None:
        if (
            row["event_id"],
            row["delivery_id"],
            row["seat_id"],
            row["workspace_id"],
            row["surface_id"],
            row["title"],
            row["subtitle"],
            row["body"],
        ) != (
            event_id,
            delivery_id,
            seat_id,
            workspace_id,
            surface_id,
            title,
            subtitle,
            body,
        ):
            raise NotificationLifecycleError("event_id_claim_contradiction")

    @staticmethod
    def _token(event_id: str, delivery_id: str) -> str:
        digest = hashlib.sha256(f"{event_id}\0{delivery_id}".encode()).hexdigest()[:16]
        return f"YGR-{digest}"

    @staticmethod
    def _record(row: sqlite3.Row) -> NotificationRecord:
        notification_id = row["notification_id"]
        if not isinstance(notification_id, str):
            raise NotificationLifecycleError("notification_not_active")
        return NotificationRecord(
            event_id=row["event_id"],
            delivery_id=row["delivery_id"],
            seat_id=row["seat_id"],
            notification_id=notification_id,
            workspace_id=row["workspace_id"],
            surface_id=row["surface_id"],
            correlation_token=row["correlation_token"],
            created_at=row["created_at"],
            expires_at=row["expires_at"],
        )


def validate_hook_effects(
    before: dict[str, object],
    after: dict[str, object],
    *,
    explicit_mark_read: bool = False,
) -> None:
    """Reject a hook result that silently clears durable/user-visible effects."""
    before_effects = before.get("effects")
    after_effects = after.get("effects")
    if not isinstance(before_effects, dict) or not isinstance(after_effects, dict):
        raise NotificationLifecycleError("notification_hook_effects_shape_invalid")
    if (
        before_effects.get("desktop") is True
        and after_effects.get("desktop") is not True
    ):
        raise NotificationLifecycleError("notification_hook_removed_desktop")
    if (
        not explicit_mark_read
        and before_effects.get("markUnread") is True
        and after_effects.get("markUnread") is not True
    ):
        raise NotificationLifecycleError("notification_hook_removed_mark_unread")
