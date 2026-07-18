"""Durable injection journal.

Injecting into a live session is a **local side effect that cannot be undone**.
Once the text is in the pane, no amount of remote retrying takes it back. That
single fact drives everything here:

- the record of intent is written and **fsynced before** the pane is touched, so a
  crash between the effect and its record can never read as "never injected";
- a redelivery of a known ``delivery_id`` re-emits the journaled outcome instead
  of injecting again;
- a row left at ``prepared`` after a crash is **ambiguous**, not failed. It is
  reconciled against observed evidence when possible and otherwise held and
  surfaced. It is never blind re-injected, because that is precisely how one
  message becomes two model turns.

Keyed on ``delivery_id``, never ``event_id``: one broadcast event fans out to many
recipient deliveries, so an ``event_id`` key would treat the second recipient's
legitimate delivery as a duplicate and silently drop it.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS injection_journal (
    delivery_id   TEXT PRIMARY KEY,
    binding_id    TEXT NOT NULL,
    seat_id       TEXT NOT NULL,
    marker        TEXT NOT NULL,
    state         TEXT NOT NULL,
    prepared_at   REAL NOT NULL,
    settled_at    REAL,
    source_events TEXT NOT NULL DEFAULT '[]',
    detail        TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_journal_state ON injection_journal(state);
"""


class JournalState(StrEnum):
    """Lifecycle of one injection attempt."""

    PREPARED = "prepared"
    """Intent recorded, pane not yet touched — or touched and unconfirmed. On
    recovery this means **the effect may or may not have fired**."""

    INJECTED = "injected"
    """The effect fired and local evidence confirms it."""

    ACKED = "acked"
    """The core acknowledged the receipt; the attempt is closed."""

    AMBIGUOUS = "ambiguous"
    """Terminal-but-unproven. Held and surfaced, never retried."""


TERMINAL: frozenset[JournalState] = frozenset(
    {JournalState.ACKED, JournalState.AMBIGUOUS}
)

#: The lifecycle, enforced rather than merely documented. A comment describing a
#: state machine that the code does not check is an invitation to violate it.
#: Note what is deliberately absent: ``injected -> ambiguous`` would downgrade a
#: proven injection to unproven, and ``prepared -> acked`` would skip the step
#: that records the effect actually fired.
LEGAL_TRANSITIONS: dict[JournalState, frozenset[JournalState]] = {
    JournalState.PREPARED: frozenset({JournalState.INJECTED, JournalState.AMBIGUOUS}),
    JournalState.INJECTED: frozenset({JournalState.ACKED}),
    JournalState.ACKED: frozenset(),
    JournalState.AMBIGUOUS: frozenset(),
}


class JournalError(RuntimeError):
    """Raised when the journal cannot guarantee its own durability."""


@dataclass(frozen=True, slots=True)
class JournalRow:
    delivery_id: str
    binding_id: str
    seat_id: str
    marker: str
    state: JournalState
    prepared_at: float
    settled_at: float | None
    source_events: tuple[str, ...]
    detail: str

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL

    @property
    def needs_reconciliation(self) -> bool:
        """A row stuck at ``prepared`` is the crash-window case."""
        return self.state is JournalState.PREPARED


class InjectionJournal:
    """Durable, single-host record of injection attempts.

    Deliberately not a second mailbox. It stores only what is needed to make
    at-least-once *network* delivery safe against exactly-once *local* injection,
    and it holds no message content — only the marker that identifies an attempt.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.path, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        # WAL keeps readers from blocking the write path; FULL synchronous is the
        # point of the whole module — a journal that loses its last write during
        # the crash it exists to survive is worse than no journal, because it
        # reports "never injected" with confidence.
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.executescript(_SCHEMA)

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> InjectionJournal:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- write path -----------------------------------------------------------

    def prepare(
        self,
        *,
        delivery_id: str,
        binding_id: str,
        seat_id: str,
        marker: str,
        now: float,
    ) -> JournalRow:
        """Record intent **before** the pane is touched.

        Raises if a row already exists: a caller reaching this point twice for the
        same delivery has skipped the duplicate check, and silently overwriting
        would erase the evidence that an effect may already have fired.
        """
        # The uniqueness check is the INSERT itself. Reading first and inserting
        # second is a time-of-check/time-of-use race: two concurrent prepares for
        # the same delivery both see "absent" and one gets a raw IntegrityError
        # from the driver instead of the documented JournalError. Let the primary
        # key decide, inside the transaction, and translate the failure.
        try:
            with self._transaction():
                self._db.execute(
                    "INSERT INTO injection_journal"
                    " (delivery_id, binding_id, seat_id, marker, state, prepared_at)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        delivery_id,
                        binding_id,
                        seat_id,
                        marker,
                        JournalState.PREPARED.value,
                        now,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            existing = self.get(delivery_id)
            state = existing.state if existing else "unknown"
            raise JournalError(
                f"delivery {delivery_id} already journaled in state {state}"
            ) from exc
        row = self.get(delivery_id)
        assert row is not None
        return row

    def settle(
        self,
        *,
        delivery_id: str,
        state: JournalState,
        now: float,
        source_events: tuple[str, ...] = (),
        detail: str = "",
    ) -> JournalRow:
        """Move a prepared row to its outcome."""
        if state is JournalState.PREPARED:
            raise JournalError("settle() cannot move a row back to prepared")

        row = self.get(delivery_id)
        if row is None:
            raise JournalError(f"delivery {delivery_id} was never prepared")
        if row.state is not state and state not in LEGAL_TRANSITIONS[row.state]:
            raise JournalError(
                f"illegal transition {row.state} -> {state} for delivery {delivery_id}"
            )

        # Evidence is append-only, and now actually is. Replacing whenever new
        # events are supplied means a later settle carrying a SUBSET — or a
        # different ordering — silently drops what an earlier one proved. Append
        # what is new, preserving first-seen order, and never remove.
        merged_events = list(row.source_events)
        for event in source_events:
            if event not in merged_events:
                merged_events.append(event)
        merged_events = tuple(merged_events)
        merged_detail = detail or row.detail

        with self._transaction():
            self._db.execute(
                "UPDATE injection_journal"
                " SET state = ?, settled_at = ?, source_events = ?, detail = ?"
                " WHERE delivery_id = ?",
                (
                    state.value,
                    now,
                    json.dumps(list(merged_events)),
                    merged_detail,
                    delivery_id,
                ),
            )
        settled = self.get(delivery_id)
        assert settled is not None
        return settled

    # -- read path ------------------------------------------------------------

    def get(self, delivery_id: str) -> JournalRow | None:
        cur = self._db.execute(
            "SELECT * FROM injection_journal WHERE delivery_id = ?", (delivery_id,)
        )
        row = cur.fetchone()
        return self._to_row(row) if row is not None else None

    def unsettled(self) -> tuple[JournalRow, ...]:
        """Rows left at ``prepared`` — the crash-window set, in preparation order."""
        cur = self._db.execute(
            "SELECT * FROM injection_journal WHERE state = ? ORDER BY prepared_at",
            (JournalState.PREPARED.value,),
        )
        return tuple(self._to_row(r) for r in cur.fetchall())

    @staticmethod
    def _to_row(raw: sqlite3.Row) -> JournalRow:
        return JournalRow(
            delivery_id=raw["delivery_id"],
            binding_id=raw["binding_id"],
            seat_id=raw["seat_id"],
            marker=raw["marker"],
            state=JournalState(raw["state"]),
            prepared_at=raw["prepared_at"],
            settled_at=raw["settled_at"],
            source_events=tuple(json.loads(raw["source_events"])),
            detail=raw["detail"],
        )

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        self._db.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self._db.execute("ROLLBACK")
            raise
        else:
            self._db.execute("COMMIT")
            self._fsync_dir()

    def _fsync_dir(self) -> None:
        """Durability of the file entry itself, not just its contents.

        SQLite fsyncs the database file, but on a crash the directory entry can
        still be stale. This is cheap and it is the difference between a journal
        that survives the event it exists for and one that merely usually does.
        """
        fd = os.open(self.path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        except OSError:  # pragma: no cover - platform dependent
            pass
        finally:
            os.close(fd)


def default_path() -> Path:
    """Host-local journal location. Never shared between hosts.

    Deliberately NOT the system temp directory: tmp is frequently tmpfs-backed or
    periodically cleaned, so a journal living there would not survive the reboot
    or power loss it exists to survive — a durability guarantee that evaporates
    under exactly its own use case. Falls back to the XDG state directory.
    """
    base = (
        os.environ.get("YATAGARASU_STATE_DIR")
        or os.environ.get("XDG_STATE_HOME")
        or (Path.home() / ".local" / "state")
    )
    return Path(base) / "yatagarasu" / "cmux-injection-journal.sqlite"
