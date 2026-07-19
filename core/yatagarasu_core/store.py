"""Small transactional store for the receipt reducer's durable contract."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from pathlib import Path

from .proofs import parse_timestamp, proof_storage_fields
from .types import (
    BindingState,
    CorrelationRule,
    Delivery,
    DeliveryMode,
    DeliveryState,
    Disposition,
    EvidenceClass,
    ProofMethodRegistration,
    ProviderKind,
    Receipt,
    SessionBinding,
    SourceKind,
)


class ConcurrentTransitionError(RuntimeError):
    """The delivery changed after validation but before receipt commit."""


class BindingConflictError(RuntimeError):
    """A second authoritative session attempted to become active."""


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
                kind TEXT NOT NULL CHECK (kind IN ('session-transport', 'comms-view')),
                health TEXT NOT NULL DEFAULT 'healthy'
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
                session_id TEXT,
                marker_signature TEXT,
                source_event_chain TEXT NOT NULL DEFAULT '[]',
                turn_id TEXT,
                UNIQUE (provider_id, source_event_id)
            );

            CREATE TABLE IF NOT EXISTS session_bindings (
                binding_id TEXT PRIMARY KEY,
                recipient_id TEXT NOT NULL,
                provider_id TEXT NOT NULL REFERENCES providers(provider_id),
                adapter_instance_id TEXT NOT NULL,
                harness TEXT NOT NULL,
                session_id TEXT NOT NULL,
                established_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                state TEXT NOT NULL CHECK (state IN ('active', 'revoked', 'superseded'))
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_one_active_binding_per_recipient
                ON session_bindings(recipient_id) WHERE state = 'active';

            CREATE TABLE IF NOT EXISTS binding_proof_methods (
                binding_id TEXT NOT NULL REFERENCES session_bindings(binding_id),
                proof_method TEXT NOT NULL,
                source_kind TEXT NOT NULL CHECK (source_kind IN ('direct-hook', 'event-bus')),
                source_instance_id TEXT NOT NULL,
                correlation_rule TEXT NOT NULL,
                evidence_classes TEXT NOT NULL,
                PRIMARY KEY (binding_id, proof_method)
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

            CREATE TABLE IF NOT EXISTS receipt_rejections (
                rejection_id INTEGER PRIMARY KEY AUTOINCREMENT,
                receipt_id TEXT NOT NULL,
                delivery_id TEXT NOT NULL,
                provider_id TEXT NOT NULL,
                evidence_class TEXT NOT NULL,
                proof_method TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                reason TEXT NOT NULL
            );
            """
        )
        self._ensure_column("providers", "health", "TEXT NOT NULL DEFAULT 'healthy'")
        self._ensure_column("receipts", "session_id", "TEXT")
        self._ensure_column("receipts", "marker_signature", "TEXT")
        self._ensure_column(
            "receipts", "source_event_chain", "TEXT NOT NULL DEFAULT '[]'"
        )
        self._ensure_column("receipts", "turn_id", "TEXT")
        self.connection.commit()

    def _ensure_column(self, table: str, column: str, declaration: str) -> None:
        columns = {
            row[1] for row in self.connection.execute(f"PRAGMA table_info({table})")
        }
        if column not in columns:
            self.connection.execute(
                f"ALTER TABLE {table} ADD COLUMN {column} {declaration}"
            )

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

    def register_session_binding(self, binding: SessionBinding) -> None:
        """Register one active authoritative session, failing closed on conflict."""
        self._validate_session_binding(binding)
        try:
            with self.connection:
                self._insert_session_binding(binding)
        except sqlite3.IntegrityError as exc:
            raise BindingConflictError(
                f"active binding conflict for recipient {binding.recipient_id}"
            ) from exc

    def supersede_session_binding(
        self, old_binding_id: str, replacement: SessionBinding
    ) -> None:
        """Atomically retire one binding and install its replacement."""
        old = self.session_binding(old_binding_id)
        if old is None or old["state"] != BindingState.ACTIVE.value:
            raise BindingConflictError("binding to supersede is not active")
        if old["recipient_id"] != replacement.recipient_id:
            raise BindingConflictError("replacement owns a different recipient")
        self._validate_session_binding(replacement)
        with self.connection:
            self.connection.execute(
                "UPDATE session_bindings SET state = ? WHERE binding_id = ?",
                (BindingState.SUPERSEDED.value, old_binding_id),
            )
            self._insert_session_binding(replacement)

    def _validate_session_binding(self, binding: SessionBinding) -> None:
        if binding.state is not BindingState.ACTIVE:
            raise ValueError("new session binding must be active")
        required_text = (
            binding.binding_id,
            binding.recipient_id,
            binding.provider_id,
            binding.adapter_instance_id,
            binding.harness,
            binding.session_id,
        )
        if any(not value for value in required_text):
            raise ValueError("binding identity fields must not be empty")
        if parse_timestamp(binding.expires_at) <= parse_timestamp(
            binding.established_at
        ):
            raise ValueError("binding expiry must follow establishment")
        provider = self.provider(binding.provider_id)
        if provider is None or provider["kind"] != ProviderKind.SESSION_TRANSPORT.value:
            raise ValueError("binding provider must be a registered session transport")
        if not binding.proof_methods:
            raise ValueError("binding must declare at least one proof method")
        method_names = [method.proof_method for method in binding.proof_methods]
        if len(set(method_names)) != len(method_names) or any(
            not name for name in method_names
        ):
            raise ValueError("binding proof methods must be unique and non-empty")
        for method in binding.proof_methods:
            if not method.source_instance_id or not method.evidence_classes:
                raise ValueError("proof method source and evidence must not be empty")
            if any(
                not self.provider_declares(binding.provider_id, evidence)
                for evidence in method.evidence_classes
            ):
                raise ValueError("proof method evidence is not declared by provider")

    def revoke_session_binding(self, binding_id: str) -> None:
        changed = self.connection.execute(
            "UPDATE session_bindings SET state = ? WHERE binding_id = ? AND state = ?",
            (BindingState.REVOKED.value, binding_id, BindingState.ACTIVE.value),
        ).rowcount
        if changed != 1:
            raise ValueError("binding_not_active")
        self.connection.commit()

    def _insert_session_binding(self, binding: SessionBinding) -> None:
        self.connection.execute(
            """INSERT INTO session_bindings
               (binding_id, recipient_id, provider_id, adapter_instance_id, harness,
                session_id, established_at, expires_at, state)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                binding.binding_id,
                binding.recipient_id,
                binding.provider_id,
                binding.adapter_instance_id,
                binding.harness,
                binding.session_id,
                binding.established_at,
                binding.expires_at,
                binding.state.value,
            ),
        )
        for method in binding.proof_methods:
            self.connection.execute(
                """INSERT INTO binding_proof_methods
                   (binding_id, proof_method, source_kind, source_instance_id,
                    correlation_rule, evidence_classes)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    binding.binding_id,
                    method.proof_method,
                    method.source_kind.value,
                    method.source_instance_id,
                    method.correlation_rule.value,
                    ",".join(sorted(item.value for item in method.evidence_classes)),
                ),
            )

    def session_binding(self, binding_id: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM session_bindings WHERE binding_id = ?", (binding_id,)
        ).fetchone()

    def binding_proof_method(
        self, binding_id: str, proof_method: str
    ) -> ProofMethodRegistration | None:
        row = self.connection.execute(
            """SELECT * FROM binding_proof_methods
               WHERE binding_id = ? AND proof_method = ?""",
            (binding_id, proof_method),
        ).fetchone()
        if row is None:
            return None
        return ProofMethodRegistration(
            proof_method=row["proof_method"],
            source_kind=SourceKind(row["source_kind"]),
            source_instance_id=row["source_instance_id"],
            correlation_rule=CorrelationRule(row["correlation_rule"]),
            evidence_classes=frozenset(
                EvidenceClass(value) for value in row["evidence_classes"].split(",")
            ),
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

    def mark_provider_degraded(self, provider_id: str) -> None:
        self.connection.execute(
            "UPDATE providers SET health = 'degraded' WHERE provider_id = ?",
            (provider_id,),
        )
        self.connection.commit()

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

    def session_entry_chain(self, delivery_id: str) -> str | None:
        row = self.connection.execute(
            """SELECT source_event_chain FROM receipts
               WHERE delivery_id = ?
                 AND evidence_class IN ('harness.prompt_accepted', 'harness.turn_started')
               ORDER BY rowid DESC LIMIT 1""",
            (delivery_id,),
        ).fetchone()
        return row["source_event_chain"] if row else None

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
        session_id, marker_signature, source_event_chain, turn_id = (
            proof_storage_fields(receipt.proof)
        )
        with self.connection:
            self.connection.execute(
                """INSERT INTO receipts
                   (receipt_id, provider_id, source_event_id, event_id, delivery_id,
                    attempt_id, binding_id, evidence_class, proof_method, observed_at,
                    disposition, platform_principal_id, platform_message_id,
                    authored_by_provider, infrastructure_event, session_id,
                    marker_signature, source_event_chain, turn_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
                    session_id,
                    marker_signature,
                    source_event_chain,
                    turn_id,
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

    def record_rejection(self, receipt: Receipt, reason: str) -> None:
        self.connection.execute(
            """INSERT INTO receipt_rejections
               (receipt_id, delivery_id, provider_id, evidence_class, proof_method,
                observed_at, reason) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                receipt.receipt_id,
                receipt.delivery_id,
                receipt.evidence_provider_id,
                receipt.evidence_class.value,
                receipt.proof_method,
                receipt.observed_at,
                reason,
            ),
        )
        self.connection.commit()

    def rejections_for(self, delivery_id: str) -> list[dict[str, object]]:
        rows = self.connection.execute(
            """SELECT * FROM receipt_rejections
               WHERE delivery_id = ? ORDER BY rejection_id""",
            (delivery_id,),
        ).fetchall()
        return [dict(row) for row in rows]
