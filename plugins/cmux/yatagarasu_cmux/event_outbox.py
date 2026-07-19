"""Durable source-event outbox and cursor for the CMUX resident."""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS stream_cursor (
    source_instance_id TEXT PRIMARY KEY,
    boot_id            TEXT NOT NULL,
    seq                INTEGER NOT NULL CHECK (seq >= 0)
);

CREATE TABLE IF NOT EXISTS source_event_outbox (
    source_instance_id TEXT NOT NULL,
    boot_id            TEXT NOT NULL,
    seq                INTEGER NOT NULL CHECK (seq >= 0),
    source_event_id    TEXT NOT NULL,
    event_name         TEXT NOT NULL,
    event_json         TEXT NOT NULL,
    PRIMARY KEY (source_instance_id, boot_id, seq),
    UNIQUE (source_instance_id, source_event_id)
);

CREATE TABLE IF NOT EXISTS stream_audit (
    audit_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    source_instance_id TEXT NOT NULL,
    kind               TEXT NOT NULL,
    boot_id            TEXT NOT NULL,
    seq                INTEGER NOT NULL CHECK (seq >= 0),
    detail_json        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS stream_snapshot_baseline (
    source_instance_id TEXT NOT NULL,
    method             TEXT NOT NULL,
    boot_id            TEXT NOT NULL,
    seq                INTEGER NOT NULL CHECK (seq >= 0),
    snapshot_json      TEXT NOT NULL,
    captured_at        TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (source_instance_id, method)
);
"""


def default_event_outbox_path() -> Path:
    """Return the resident outbox path, honoring the shared state directory."""
    state_root = os.environ.get("YATAGARASU_STATE_DIR")
    if state_root:
        return Path(state_root) / "cmux-event-outbox.sqlite"
    return Path.home() / ".local" / "state" / "yatagarasu" / "cmux-event-outbox.sqlite"


class OutboxError(RuntimeError):
    """The durable source-event plane cannot classify an observation safely."""


class CommitDisposition(StrEnum):
    """Result of atomically committing one derived event and cursor."""

    INSERTED = "inserted"
    DUPLICATE = "duplicate"


@dataclass(frozen=True, slots=True)
class EventCursor:
    """One host-resident stream position; all three fields are identity."""

    source_instance_id: str
    boot_id: str
    seq: int


@dataclass(frozen=True, slots=True)
class DerivedEvent:
    """Content-free event metadata safe to persist for receipt generation."""

    source_instance_id: str
    boot_id: str
    seq: int
    source_event_id: str
    event_name: str
    event_json: str

    @property
    def cursor(self) -> EventCursor:
        return EventCursor(self.source_instance_id, self.boot_id, self.seq)


@dataclass(frozen=True, slots=True)
class SnapshotBaseline:
    """One literal command result used to rebuild state after a replay gap."""

    method: str
    snapshot_json: str


class EventOutbox:
    """SQLite outbox whose event insert and cursor advance share one commit."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.path, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.executescript(_SCHEMA)

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> EventOutbox:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def cursor(self, source_instance_id: str) -> EventCursor | None:
        row = self._db.execute(
            "SELECT * FROM stream_cursor WHERE source_instance_id = ?",
            (source_instance_id,),
        ).fetchone()
        if row is None:
            return None
        return EventCursor(row["source_instance_id"], row["boot_id"], row["seq"])

    def commit_event(
        self, event: DerivedEvent, *, permit_boot_change: bool
    ) -> CommitDisposition:
        """Commit the derived outbox row and cursor in one SQLite transaction."""
        self._validate_event(event)
        self._db.execute("BEGIN IMMEDIATE")
        try:
            existing = self._db.execute(
                """SELECT * FROM source_event_outbox
                   WHERE source_instance_id = ? AND boot_id = ? AND seq = ?""",
                (event.source_instance_id, event.boot_id, event.seq),
            ).fetchone()
            if existing is not None:
                if not self._same_event(existing, event):
                    raise OutboxError("cursor triple already names different evidence")
                self._db.execute("COMMIT")
                return CommitDisposition.DUPLICATE

            id_owner = self._db.execute(
                """SELECT boot_id, seq FROM source_event_outbox
                   WHERE source_instance_id = ? AND source_event_id = ?""",
                (event.source_instance_id, event.source_event_id),
            ).fetchone()
            if id_owner is not None:
                raise OutboxError("source event ID was replayed under a new cursor")

            cursor = self.cursor(event.source_instance_id)
            if cursor is not None:
                if cursor.boot_id != event.boot_id and not permit_boot_change:
                    raise OutboxError("boot changed without a declared resume gap")
                if cursor.boot_id == event.boot_id and event.seq <= cursor.seq:
                    raise OutboxError(
                        "unseen event does not advance the durable cursor"
                    )

            self._db.execute(
                """INSERT INTO source_event_outbox
                   (source_instance_id, boot_id, seq, source_event_id,
                    event_name, event_json) VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    event.source_instance_id,
                    event.boot_id,
                    event.seq,
                    event.source_event_id,
                    event.event_name,
                    event.event_json,
                ),
            )
            self._upsert_cursor(event.cursor)
            self._db.execute("COMMIT")
        except BaseException:
            self._db.execute("ROLLBACK")
            raise
        self._fsync_dir()
        return CommitDisposition.INSERTED

    def record_resnapshot(
        self,
        cursor: EventCursor,
        *,
        snapshots: tuple[SnapshotBaseline, ...],
        replay_count: int,
    ) -> None:
        """Record a completed gap snapshot and its new resume position atomically."""
        if not snapshots:
            raise OutboxError("gap recovery requires literal snapshot commands")
        commands = tuple(snapshot.method for snapshot in snapshots)
        if len(commands) != len(set(commands)):
            raise OutboxError("gap recovery snapshot commands must be unique")
        for snapshot in snapshots:
            if not snapshot.method:
                raise OutboxError("gap recovery snapshot method is empty")
            try:
                json.loads(snapshot.snapshot_json)
            except json.JSONDecodeError as exc:
                raise OutboxError("gap recovery snapshot is not valid JSON") from exc
        current = self.cursor(cursor.source_instance_id)
        if (
            current is not None
            and current.boot_id == cursor.boot_id
            and cursor.seq < current.seq
        ):
            raise OutboxError("gap snapshot cannot regress the durable cursor")
        detail = json.dumps(
            {"commands": list(commands), "replay_count": replay_count},
            separators=(",", ":"),
            sort_keys=True,
        )
        self._db.execute("BEGIN IMMEDIATE")
        try:
            for snapshot in snapshots:
                self._db.execute(
                    """INSERT INTO stream_snapshot_baseline
                       (source_instance_id, method, boot_id, seq, snapshot_json)
                       VALUES (?, ?, ?, ?, ?)
                       ON CONFLICT(source_instance_id, method) DO UPDATE SET
                         boot_id = excluded.boot_id,
                         seq = excluded.seq,
                         snapshot_json = excluded.snapshot_json,
                         captured_at = CURRENT_TIMESTAMP""",
                    (
                        cursor.source_instance_id,
                        snapshot.method,
                        cursor.boot_id,
                        cursor.seq,
                        snapshot.snapshot_json,
                    ),
                )
            self._db.execute(
                """INSERT INTO stream_audit
                   (source_instance_id, kind, boot_id, seq, detail_json)
                   VALUES (?, 'gap_resnapshot', ?, ?, ?)""",
                (cursor.source_instance_id, cursor.boot_id, cursor.seq, detail),
            )
            self._upsert_cursor(cursor)
            self._db.execute("COMMIT")
        except BaseException:
            self._db.execute("ROLLBACK")
            raise
        self._fsync_dir()

    def record_reconnect_replay(
        self,
        cursor: EventCursor,
        *,
        requested_after_seq: int,
        replay_count: int,
        inserted_count: int,
        duplicate_count: int,
        disconnect_at_seq: int | None,
    ) -> None:
        """Persist literal replay accounting without moving the proven cursor."""
        current = self.cursor(cursor.source_instance_id)
        if current != cursor:
            raise OutboxError("reconnect audit cursor is not the durable winner")
        detail = json.dumps(
            {
                "duplicate_count": duplicate_count,
                "inserted_count": inserted_count,
                "replay_count": replay_count,
                "requested_after_seq": requested_after_seq,
                "disconnect_at_seq": disconnect_at_seq,
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        self._db.execute("BEGIN IMMEDIATE")
        try:
            self._db.execute(
                """INSERT INTO stream_audit
                   (source_instance_id, kind, boot_id, seq, detail_json)
                   VALUES (?, 'reconnect_replay', ?, ?, ?)""",
                (cursor.source_instance_id, cursor.boot_id, cursor.seq, detail),
            )
            self._db.execute("COMMIT")
        except BaseException:
            self._db.execute("ROLLBACK")
            raise
        self._fsync_dir()

    def outbox_rows(self, source_instance_id: str) -> tuple[dict[str, object], ...]:
        rows = self._db.execute(
            """SELECT * FROM source_event_outbox
               WHERE source_instance_id = ? ORDER BY rowid""",
            (source_instance_id,),
        ).fetchall()
        return tuple(dict(row) for row in rows)

    def audit_rows(self, source_instance_id: str) -> tuple[dict[str, object], ...]:
        rows = self._db.execute(
            "SELECT * FROM stream_audit WHERE source_instance_id = ? ORDER BY audit_id",
            (source_instance_id,),
        ).fetchall()
        return tuple(dict(row) for row in rows)

    def snapshot_rows(self, source_instance_id: str) -> tuple[dict[str, object], ...]:
        """Return the latest durable baseline for each literal snapshot method."""
        rows = self._db.execute(
            """SELECT * FROM stream_snapshot_baseline
               WHERE source_instance_id = ? ORDER BY method""",
            (source_instance_id,),
        ).fetchall()
        return tuple(dict(row) for row in rows)

    def _upsert_cursor(self, cursor: EventCursor) -> None:
        self._db.execute(
            """INSERT INTO stream_cursor (source_instance_id, boot_id, seq)
               VALUES (?, ?, ?)
               ON CONFLICT(source_instance_id) DO UPDATE SET
                 boot_id = excluded.boot_id,
                 seq = excluded.seq""",
            (cursor.source_instance_id, cursor.boot_id, cursor.seq),
        )

    @staticmethod
    def _same_event(row: sqlite3.Row, event: DerivedEvent) -> bool:
        return all(
            (
                row["source_event_id"] == event.source_event_id,
                row["event_name"] == event.event_name,
                row["event_json"] == event.event_json,
            )
        )

    @staticmethod
    def _validate_event(event: DerivedEvent) -> None:
        if (
            not event.source_instance_id
            or not event.boot_id
            or isinstance(event.seq, bool)
            or event.seq < 0
            or not event.source_event_id
            or not event.event_name
        ):
            raise OutboxError("source event identity is incomplete")
        try:
            value = json.loads(event.event_json)
        except json.JSONDecodeError as exc:
            raise OutboxError("derived event is not valid JSON") from exc
        if not isinstance(value, dict):
            raise OutboxError("derived event must be a JSON object")

    def _fsync_dir(self) -> None:
        fd = os.open(self.path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        except OSError:  # pragma: no cover - platform dependent
            pass
        finally:
            os.close(fd)
