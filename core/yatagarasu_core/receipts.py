"""Evidence-bound receipt reduction for both Round-1 delivery modes."""

from __future__ import annotations

import sqlite3

from .proofs import (
    MarkerAuthority,
    parse_timestamp,
    proof_storage_fields,
    source_chain_json,
    validate_session_proof,
)
from .store import ConcurrentTransitionError, CoreStore
from .types import (
    BindingState,
    Delivery,
    DeliveryMode,
    DeliveryState,
    Disposition,
    EvidenceClass,
    ProviderKind,
    Receipt,
    ReceiptResult,
    SessionProof,
)

PARTICIPANT_EVIDENCE = {
    EvidenceClass.PARTICIPANT_REPLY_AUTHORED,
    EvidenceClass.PARTICIPANT_REACTION_AUTHORED,
}
SESSION_PROOF_EVIDENCE = {
    EvidenceClass.HARNESS_PROMPT_ACCEPTED,
    EvidenceClass.HARNESS_TURN_STARTED,
    EvidenceClass.HARNESS_TURN_COMPLETED,
}


class ReceiptReducer:
    """Accept evidence and advance one delivery no farther than it proves."""

    def __init__(
        self, store: CoreStore, marker_authority: MarkerAuthority | None = None
    ) -> None:
        self.store = store
        self.marker_authority = marker_authority

    def submit(self, receipt: Receipt) -> ReceiptResult:
        if not receipt.proof_method or not receipt.observed_at:
            return self._reject("audit_fields_required", receipt)
        if receipt.proof is not None and not isinstance(receipt.proof, SessionProof):
            return self._reject("session_proof_shape_invalid", receipt)
        existing_result = self._existing_receipt_result(receipt)
        if existing_result is not None:
            return existing_result

        delivery = self.store.get_delivery(receipt.delivery_id)
        if delivery is None:
            return self._reject("delivery_not_found", receipt)
        key_error = self._validate_keys(receipt, delivery)
        if key_error:
            return self._reject(key_error, receipt)

        provider = self.store.provider(receipt.evidence_provider_id)
        if provider is None:
            return self._reject("provider_not_registered", receipt)
        if not self.store.provider_declares(
            receipt.evidence_provider_id, receipt.evidence_class
        ):
            return self._reject("evidence_class_not_declared", receipt)

        if receipt.evidence_class in PARTICIPANT_EVIDENCE:
            validation_error = self._validate_participant(
                receipt, delivery, provider["kind"]
            )
            if validation_error:
                return self._reject(validation_error, receipt)
        elif (
            delivery.delivery_mode is DeliveryMode.CHANNEL_NATIVE
            and receipt.evidence_class
            not in {
                EvidenceClass.TRANSPORT_SUBMIT_ACK,
            }
        ):
            return self._reject("evidence_class_wrong_delivery_mode", receipt)

        transition = self._transition(receipt, delivery, ProviderKind(provider["kind"]))
        if isinstance(transition, str):
            return self._reject(transition, receipt)
        next_state, disposition = transition

        if receipt.evidence_class in SESSION_PROOF_EVIDENCE:
            proof_error = self._validate_session_receipt(receipt, delivery)
            if proof_error:
                return self._reject(proof_error, receipt)

        try:
            self.store.accept_receipt(
                receipt=receipt,
                delivery=delivery,
                next_state=next_state,
                disposition=disposition,
            )
        except ConcurrentTransitionError:
            return self._reject("delivery_state_changed", receipt)
        except sqlite3.IntegrityError:
            return self._recover_integrity_collision(receipt)
        return ReceiptResult("accepted", state=next_state, disposition=disposition)

    def _existing_receipt_result(self, receipt: Receipt) -> ReceiptResult | None:
        existing = self.store.receipt_record(receipt.receipt_id)
        if existing is None:
            return None
        if not self._same_receipt(existing, receipt):
            self.store.mark_provider_degraded(receipt.evidence_provider_id)
            return self._reject("receipt_id_contradiction", receipt)
        delivery = self.store.get_delivery(receipt.delivery_id)
        return ReceiptResult(
            "duplicate",
            state=delivery.state if delivery else None,
            disposition=delivery.disposition if delivery else None,
        )

    def _recover_integrity_collision(self, receipt: Receipt) -> ReceiptResult:
        """Re-read durable winners after a uniqueness/FK race.

        The failed transaction has rolled back. A concurrent winner, if one
        exists, is now the authority for the deterministic reducer verdict.
        """
        existing_result = self._existing_receipt_result(receipt)
        if existing_result is not None:
            return existing_result
        if receipt.source_event_id and self.store.source_seen(
            receipt.evidence_provider_id, receipt.source_event_id
        ):
            return self._reject("source_event_replayed", receipt)
        if self.store.get_delivery(receipt.delivery_id) is None:
            return self._reject("delivery_not_found", receipt)
        if self.store.provider(receipt.evidence_provider_id) is None:
            return self._reject("provider_not_registered", receipt)
        return self._reject("storage_integrity_error", receipt)

    @staticmethod
    def _same_receipt(existing, receipt: Receipt) -> bool:
        """A duplicate ID is idempotent only when its semantic claim is equal."""
        session_id, marker_signature, source_event_chain, turn_id = (
            proof_storage_fields(receipt.proof)
        )
        return all(
            (
                existing["provider_id"] == receipt.evidence_provider_id,
                existing["source_event_id"] == receipt.source_event_id,
                existing["event_id"] == receipt.event_id,
                existing["delivery_id"] == receipt.delivery_id,
                existing["attempt_id"] == receipt.attempt_id,
                existing["binding_id"] == receipt.binding_id,
                existing["evidence_class"] == receipt.evidence_class.value,
                existing["proof_method"] == receipt.proof_method,
                existing["observed_at"] == receipt.observed_at,
                existing["disposition"]
                == ReceiptReducer._canonical_disposition(receipt),
                existing["platform_principal_id"] == receipt.platform_principal_id,
                existing["platform_message_id"] == receipt.platform_message_id,
                bool(existing["authored_by_provider"]) is receipt.authored_by_provider,
                bool(existing["infrastructure_event"]) is receipt.infrastructure_event,
                existing["session_id"] == session_id,
                existing["marker_signature"] == marker_signature,
                existing["source_event_chain"] == source_event_chain,
                existing["turn_id"] == turn_id,
            )
        )

    def _validate_session_receipt(
        self, receipt: Receipt, delivery: Delivery
    ) -> str | None:
        proof = receipt.proof
        if proof is None:
            return "session_proof_required"
        if not isinstance(proof, SessionProof):
            return "session_proof_shape_invalid"
        if self.marker_authority is None:
            return "marker_authority_unavailable"

        binding = self.store.session_binding(receipt.binding_id)
        if binding is None:
            return "binding_not_registered"
        if binding["state"] != BindingState.ACTIVE.value:
            return "binding_not_active"
        if binding["provider_id"] != receipt.evidence_provider_id:
            return "provider_not_authorized_for_binding"
        if binding["recipient_id"] != delivery.recipient_id:
            return "binding_does_not_own_recipient"
        try:
            observed = parse_timestamp(receipt.observed_at)
            established = parse_timestamp(binding["established_at"])
            expires = parse_timestamp(binding["expires_at"])
        except ValueError:
            return "binding_timestamp_invalid"
        if observed < established or observed >= expires:
            return "binding_expired_or_not_yet_active"
        if proof.session_id != binding["session_id"]:
            return "authoritative_session_mismatch"

        registration = self.store.binding_proof_method(
            receipt.binding_id, receipt.proof_method
        )
        if registration is None:
            return "proof_method_not_registered_for_binding"
        proof_error = validate_session_proof(
            proof=proof,
            delivery=delivery,
            evidence_class=receipt.evidence_class,
            registration=registration,
            marker_authority=self.marker_authority,
            observed_at=receipt.observed_at,
        )
        if proof_error:
            return proof_error
        if receipt.evidence_class is EvidenceClass.HARNESS_TURN_COMPLETED:
            accepted_chain = self.store.session_entry_chain(delivery.delivery_id)
            if accepted_chain is None or accepted_chain != source_chain_json(
                proof.source_events[:-1]
            ):
                return "turn_end_does_not_close_accepted_prompt"
        if (
            not receipt.source_event_id
            or receipt.source_event_id != proof.source_events[-1].source_event_id
        ):
            return "receipt_source_event_mismatch"
        if self.store.source_seen(
            receipt.evidence_provider_id, receipt.source_event_id
        ):
            return "source_event_replayed"
        return None

    @staticmethod
    def _canonical_disposition(receipt: Receipt) -> str | None:
        if receipt.disposition is not None:
            return receipt.disposition.value
        if receipt.evidence_class is EvidenceClass.PARTICIPANT_REPLY_AUTHORED:
            return Disposition.ANSWERED.value
        if receipt.evidence_class is EvidenceClass.HARNESS_TURN_COMPLETED:
            return Disposition.COMPLETED.value
        return None

    @staticmethod
    def _validate_keys(receipt: Receipt, delivery: Delivery) -> str | None:
        if receipt.event_id != delivery.event_id:
            return "event_mismatch"
        if receipt.attempt_id != delivery.attempt_id:
            return "attempt_mismatch"
        if receipt.binding_id != delivery.binding_id:
            return "binding_mismatch"
        return None

    def _validate_participant(
        self, receipt: Receipt, delivery: Delivery, provider_kind: str
    ) -> str | None:
        if delivery.delivery_mode is not DeliveryMode.CHANNEL_NATIVE:
            return "evidence_class_wrong_delivery_mode"
        if provider_kind != ProviderKind.COMMS_VIEW.value:
            return "provider_kind_not_comms_view"
        if delivery.state is not DeliveryState.TRANSPORT_SUBMITTED:
            return "invalid_transition"
        if receipt.authored_by_provider or receipt.infrastructure_event:
            return "self_echo_or_infrastructure_event"
        if not receipt.source_event_id:
            return "source_event_id_required"
        if self.store.source_seen(
            receipt.evidence_provider_id, receipt.source_event_id
        ):
            return "source_event_replayed"
        if not receipt.platform_principal_id or not self.store.principal_matches(
            receipt.evidence_provider_id,
            receipt.platform_principal_id,
            delivery.recipient_id,
        ):
            return "principal_mismatch"
        if not receipt.platform_message_id or not self.store.message_binding_matches(
            receipt.evidence_provider_id, receipt.platform_message_id, delivery
        ):
            return "platform_message_binding_mismatch"
        return None

    def _transition(
        self, receipt: Receipt, delivery: Delivery, provider_kind: ProviderKind
    ) -> tuple[DeliveryState, Disposition | None] | str:
        evidence = receipt.evidence_class
        state = delivery.state

        if evidence is EvidenceClass.TRANSPORT_SUBMIT_ACK:
            if state is not DeliveryState.DISPATCHING:
                return "invalid_transition"
            if receipt.disposition is not None:
                return "disposition_not_allowed"
            if delivery.delivery_mode is DeliveryMode.CHANNEL_NATIVE:
                if provider_kind is not ProviderKind.COMMS_VIEW:
                    return "provider_kind_not_comms_view"
                if (
                    not receipt.platform_message_id
                    or not self.store.message_binding_matches(
                        receipt.evidence_provider_id,
                        receipt.platform_message_id,
                        delivery,
                    )
                ):
                    return "platform_message_binding_mismatch"
            elif provider_kind is not ProviderKind.SESSION_TRANSPORT:
                return "provider_kind_not_session_transport"
            return DeliveryState.TRANSPORT_SUBMITTED, None

        if evidence in {
            EvidenceClass.HARNESS_PROMPT_ACCEPTED,
            EvidenceClass.HARNESS_TURN_STARTED,
        }:
            if delivery.delivery_mode is not DeliveryMode.SESSION_BOUND:
                return "evidence_class_wrong_delivery_mode"
            if provider_kind is not ProviderKind.SESSION_TRANSPORT:
                return "provider_kind_not_session_transport"
            if state is not DeliveryState.TRANSPORT_SUBMITTED:
                return "invalid_transition"
            if receipt.disposition is not None:
                return "disposition_not_allowed"
            return DeliveryState.IN_SESSION, None

        if evidence is EvidenceClass.HARNESS_TURN_COMPLETED:
            if delivery.delivery_mode is not DeliveryMode.SESSION_BOUND:
                return "evidence_class_wrong_delivery_mode"
            if provider_kind is not ProviderKind.SESSION_TRANSPORT:
                return "provider_kind_not_session_transport"
            if state is not DeliveryState.IN_SESSION:
                return "invalid_transition"
            if receipt.disposition not in {None, Disposition.COMPLETED}:
                return "disposition_overclaim"
            return DeliveryState.PROCESSED, Disposition.COMPLETED

        if evidence is EvidenceClass.PARTICIPANT_REPLY_AUTHORED:
            # The transport-submitted state gate lives in _validate_participant,
            # which runs before this mapping for both participant classes.
            if receipt.disposition not in {None, Disposition.ANSWERED}:
                return "disposition_overclaim"
            return DeliveryState.PROCESSED, Disposition.ANSWERED

        if evidence is EvidenceClass.PARTICIPANT_REACTION_AUTHORED:
            if receipt.disposition not in {
                Disposition.ACKNOWLEDGED,
                Disposition.HELD,
            }:
                return "reaction_disposition_invalid"
            return DeliveryState.PROCESSED, receipt.disposition

        return "unsupported_evidence_class"

    def _reject(self, reason: str, receipt: Receipt) -> ReceiptResult:
        self.store.record_rejection(receipt, reason)
        return ReceiptResult("rejected", reason=reason)
