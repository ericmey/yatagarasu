"""Small transactional store for the receipt reducer's durable contract."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from pathlib import Path

from .types import (
    Delivery,
    DeliveryMode,
    DeliveryState,
    Disposition,
    EvidenceClass,
    ProviderKind,
    Receipt,
)


class ConcurrentTransitionError(RuntimeError):
    """The delivery changed after validation but before receipt commit."""


class CoreStore:
    """SQLite-backed delivery, provider, binding, receipt, and audit state.

    Content is deliberately absent: this slice persists only reducer metadata.
    """

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.connection = sqlite3.connect(str(path))
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self._migrate()

    def close(self) -> None:
        self.connection.close()

    def _migrate(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS deliveries (
                delivery_id TEXT PRIMARY KEY,
                event_id TEXT NOT NULL,
                attempt_id TEXT NOT NULL,
                binding_id TEXT NOT NULL,
                recipient_id TEXT NOT NULL,
                delivery_mode TEXT NOT NULL
                    CHECK (delivery_mode IN ('session-bound', 'channel-native')),
                state TEXT NOT NULL,
                disposition TEXT
            );

            CREATE TABLE IF NOT EXISTS providers (
                provider_id TEXT PRIMARY KEY,
                kind TEXT NOT NULL CHECK (kind IN ('session-transport', 'comms-view'))
            );

            CREATE TABLE IF NOT EXISTS provider_evidence (
                provider_id TEXT NOT NULL REFERENCES providers(provider_id),
                evidence_class TEXT NOT NULL,
                PRIMARY KEY (provider_id, evidence_class)
            );

            CREATE TABLE IF NOT EXISTS principal_bindings (
                provider_id TEXT NOT NULL REFERENCES providers(provider_id),
                platform_principal_id TEXT NOT NULL,
                recipient_id TEXT NOT NULL,
                PRIMARY KEY (provider_id, platform_principal_id)
            );

            CREATE TABLE IF NOT EXISTS platform_message_bindings (
                provider_id TEXT NOT NULL REFERENCES providers(provider_id),
                platform_message_id TEXT NOT NULL,
                event_id TEXT NOT NULL,
                delivery_id TEXT NOT NULL REFERENCES deliveries(delivery_id),
                attempt_id TEXT NOT NULL,
                PRIMARY KEY (provider_id, platform_message_id)
            );

            CREATE TABLE IF NOT EXISTS receipts (
                receipt_id TEXT PRIMARY KEY,
                provider_id TEXT NOT NULL REFERENCES providers(provider_id),
                source_event_id TEXT,
                event_id TEXT NOT NULL,
                delivery_id TEXT NOT NULL REFERENCES deliveries(delivery_id),
                attempt_id TEXT NOT NULL,
                binding_id TEXT NOT NULL,
                evidence_class TEXT NOT NULL,
                proof_method TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                disposition TEXT,
                platform_principal_id TEXT,
                platform_message_id TEXT,
                authored_by_provider INTEGER NOT NULL CHECK (authored_by_provider IN (0, 1)),
                infrastructure_event INTEGER NOT NULL CHECK (infrastructure_event IN (0, 1)),
                UNIQUE (provider_id, source_event_id)
            );

            CREATE TABLE IF NOT EXISTS audit_log (
                audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
                receipt_id TEXT NOT NULL UNIQUE REFERENCES receipts(receipt_id),
                delivery_id TEXT NOT NULL REFERENCES deliveries(delivery_id),
                from_state TEXT NOT NULL,
                to_state TEXT NOT NULL,
                disposition TEXT,
                delivery_mode TEXT NOT NULL,
                session_entry TEXT NOT NULL,
                evidence_class TEXT NOT NULL,
                proof_method TEXT NOT NULL
            );
            """
        )
        self.connection.commit()

    def add_delivery(self, delivery: Delivery) -> None:
        self.connection.execute(
            """INSERT INTO deliveries
               (delivery_id, event_id, attempt_id, binding_id, recipient_id,
                delivery_mode, state, disposition)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                delivery.delivery_id,
                delivery.event_id,
                delivery.attempt_id,
                delivery.binding_id,
                delivery.recipient_id,
                delivery.delivery_mode.value,
                delivery.state.value,
                delivery.disposition.value if delivery.disposition else None,
            ),
        )
        self.connection.commit()

    def set_dispatching(self, delivery_id: str) -> None:
        changed = self.connection.execute(
            "UPDATE deliveries SET state = ? WHERE delivery_id = ? AND state = ?",
            (DeliveryState.DISPATCHING.value, delivery_id, DeliveryState.QUEUED.value),
        ).rowcount
        if changed != 1:
            raise ValueError("delivery_not_queued")
        self.connection.commit()

    def get_delivery(self, delivery_id: str) -> Delivery | None:
        row = self.connection.execute(
            "SELECT * FROM deliveries WHERE delivery_id = ?", (delivery_id,)
        ).fetchone()
        if row is None:
            return None
        return Delivery(
            event_id=row["event_id"],
            delivery_id=row["delivery_id"],
            attempt_id=row["attempt_id"],
            binding_id=row["binding_id"],
            recipient_id=row["recipient_id"],
            delivery_mode=DeliveryMode(row["delivery_mode"]),
            state=DeliveryState(row["state"]),
            disposition=Disposition(row["disposition"]) if row["disposition"] else None,
        )

    def register_provider(
        self,
        provider_id: str,
        kind: ProviderKind,
        evidence_classes: Iterable[EvidenceClass],
    ) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT INTO providers (provider_id, kind) VALUES (?, ?)",
                (provider_id, kind.value),
            )
            self.connection.executemany(
                "INSERT INTO provider_evidence (provider_id, evidence_class) VALUES (?, ?)",
                ((provider_id, evidence.value) for evidence in evidence_classes),
            )

    def bind_principal(
        self, provider_id: str, platform_principal_id: str, recipient_id: str
    ) -> None:
        self.connection.execute(
            """INSERT INTO principal_bindings
               (provider_id, platform_principal_id, recipient_id) VALUES (?, ?, ?)""",
            (provider_id, platform_principal_id, recipient_id),
        )
        self.connection.commit()

    def bind_platform_message(
        self,
        provider_id: str,
        platform_message_id: str,
        event_id: str,
        delivery_id: str,
        attempt_id: str,
    ) -> None:
        self.connection.execute(
            """INSERT INTO platform_message_bindings
               (provider_id, platform_message_id, event_id, delivery_id, attempt_id)
               VALUES (?, ?, ?, ?, ?)""",
            (provider_id, platform_message_id, event_id, delivery_id, attempt_id),
        )
        self.connection.commit()

    def provider(self, provider_id: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM providers WHERE provider_id = ?", (provider_id,)
        ).fetchone()

    def provider_declares(self, provider_id: str, evidence: EvidenceClass) -> bool:
        return (
            self.connection.execute(
                """SELECT 1 FROM provider_evidence
               WHERE provider_id = ? AND evidence_class = ?""",
                (provider_id, evidence.value),
            ).fetchone()
            is not None
        )

    def principal_matches(
        self, provider_id: str, platform_principal_id: str, recipient_id: str
    ) -> bool:
        return (
            self.connection.execute(
                """SELECT 1 FROM principal_bindings
               WHERE provider_id = ? AND platform_principal_id = ? AND recipient_id = ?""",
                (provider_id, platform_principal_id, recipient_id),
            ).fetchone()
            is not None
        )

    def message_binding_matches(
        self,
        provider_id: str,
        platform_message_id: str,
        delivery: Delivery,
    ) -> bool:
        return (
            self.connection.execute(
                """SELECT 1 FROM platform_message_bindings
               WHERE provider_id = ? AND platform_message_id = ? AND event_id = ?
                 AND delivery_id = ? AND attempt_id = ?""",
                (
                    provider_id,
                    platform_message_id,
                    delivery.event_id,
                    delivery.delivery_id,
                    delivery.attempt_id,
                ),
            ).fetchone()
            is not None
        )

    def receipt_exists(self, receipt_id: str) -> bool:
        return (
            self.connection.execute(
                "SELECT 1 FROM receipts WHERE receipt_id = ?", (receipt_id,)
            ).fetchone()
            is not None
        )

    def receipt_record(self, receipt_id: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM receipts WHERE receipt_id = ?", (receipt_id,)
        ).fetchone()

    def source_seen(self, provider_id: str, source_event_id: str) -> bool:
        return (
            self.connection.execute(
                "SELECT 1 FROM receipts WHERE provider_id = ? AND source_event_id = ?",
                (provider_id, source_event_id),
            ).fetchone()
            is not None
        )

    def accept_receipt(
        self,
        *,
        receipt: Receipt,
        delivery: Delivery,
        next_state: DeliveryState,
        disposition: Disposition | None,
    ) -> None:
        session_entry = (
            "not_applicable"
            if delivery.delivery_mode is DeliveryMode.CHANNEL_NATIVE
            else "applicable"
        )
        with self.connection:
            self.connection.execute(
                """INSERT INTO receipts
                   (receipt_id, provider_id, source_event_id, event_id, delivery_id,
                    attempt_id, binding_id, evidence_class, proof_method, observed_at,
                    disposition, platform_principal_id, platform_message_id,
                    authored_by_provider, infrastructure_event)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    receipt.receipt_id,
                    receipt.evidence_provider_id,
                    receipt.source_event_id,
                    delivery.event_id,
                    delivery.delivery_id,
                    delivery.attempt_id,
                    delivery.binding_id,
                    receipt.evidence_class.value,
                    receipt.proof_method,
                    receipt.observed_at,
                    disposition.value if disposition else None,
                    receipt.platform_principal_id,
                    receipt.platform_message_id,
                    int(receipt.authored_by_provider),
                    int(receipt.infrastructure_event),
                ),
            )
            changed = self.connection.execute(
                """UPDATE deliveries SET state = ?, disposition = ?
                   WHERE delivery_id = ? AND state = ?""",
                (
                    next_state.value,
                    disposition.value if disposition else None,
                    delivery.delivery_id,
                    delivery.state.value,
                ),
            ).rowcount
            if changed != 1:
                raise ConcurrentTransitionError("delivery_state_changed")
            self.connection.execute(
                """INSERT INTO audit_log
                   (receipt_id, delivery_id, from_state, to_state, disposition,
                    delivery_mode, session_entry, evidence_class, proof_method)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    receipt.receipt_id,
                    delivery.delivery_id,
                    delivery.state.value,
                    next_state.value,
                    disposition.value if disposition else None,
                    delivery.delivery_mode.value,
                    session_entry,
                    receipt.evidence_class.value,
                    receipt.proof_method,
                ),
            )

    def audit_for(self, delivery_id: str) -> list[dict[str, object]]:
        rows = self.connection.execute(
            "SELECT * FROM audit_log WHERE delivery_id = ? ORDER BY audit_id",
            (delivery_id,),
        ).fetchall()
        return [dict(row) for row in rows]
