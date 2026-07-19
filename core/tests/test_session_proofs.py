"""Adversarial contract tests for Issue #24's session proof bundle."""

from __future__ import annotations

from dataclasses import replace

import pytest

from yatagarasu_core import (
    BindingConflictError,
    BindingState,
    CoreStore,
    CorrelationRule,
    Delivery,
    DeliveryMode,
    DeliveryState,
    EvidenceClass,
    MarkerAuthority,
    MarkerError,
    ProofMethodRegistration,
    ProviderKind,
    Receipt,
    ReceiptReducer,
    SessionBinding,
    SessionProof,
    SourceEventRef,
    SourceKind,
)

KEY = b"session-proof-test-key"
METHOD = "cmux.event_bus.harness_hook_relay"
SOURCE = "cmux-resident-vesper"
NOW = "2026-07-18T21:00:00Z"


class ProofCase:
    def __init__(self) -> None:
        self.store = CoreStore()
        self.authority = MarkerAuthority(KEY)
        self.reducer = ReceiptReducer(self.store, self.authority)
        self.delivery = Delivery(
            event_id="event-proof",
            delivery_id="delivery-proof",
            attempt_id="attempt-proof",
            binding_id="binding-proof",
            recipient_id="yua",
            delivery_mode=DeliveryMode.SESSION_BOUND,
        )
        self.store.add_delivery(self.delivery)
        self.store.set_dispatching(self.delivery.delivery_id)
        self.store.register_provider(
            "cmux-provider",
            ProviderKind.SESSION_TRANSPORT,
            {
                EvidenceClass.TRANSPORT_SUBMIT_ACK,
                EvidenceClass.HARNESS_PROMPT_ACCEPTED,
                EvidenceClass.HARNESS_TURN_STARTED,
                EvidenceClass.HARNESS_TURN_COMPLETED,
            },
        )
        self.binding = self.make_binding()
        self.store.register_session_binding(self.binding)
        transport = self.reducer.submit(
            self.receipt(
                "receipt-transport",
                EvidenceClass.TRANSPORT_SUBMIT_ACK,
                source_event_id="transport-submit-1",
            )
        )
        assert transport.state is DeliveryState.TRANSPORT_SUBMITTED

    def close(self) -> None:
        self.store.close()

    def make_binding(
        self,
        *,
        binding_id: str = "binding-proof",
        recipient_id: str = "yua",
        provider_id: str = "cmux-provider",
        session_id: str = "session-proof",
        state: BindingState = BindingState.ACTIVE,
    ) -> SessionBinding:
        return SessionBinding(
            binding_id=binding_id,
            recipient_id=recipient_id,
            provider_id=provider_id,
            adapter_instance_id="adapter-vesper",
            harness="codex",
            session_id=session_id,
            established_at="2026-07-18T20:00:00Z",
            expires_at="2026-07-18T22:00:00Z",
            proof_methods=(
                ProofMethodRegistration(
                    proof_method=METHOD,
                    source_kind=SourceKind.EVENT_BUS,
                    source_instance_id=SOURCE,
                    correlation_rule=CorrelationRule.CMUX_HARNESS_CHAIN,
                    evidence_classes=frozenset(
                        {
                            EvidenceClass.HARNESS_PROMPT_ACCEPTED,
                            EvidenceClass.HARNESS_TURN_STARTED,
                            EvidenceClass.HARNESS_TURN_COMPLETED,
                        }
                    ),
                ),
            ),
            state=state,
        )

    def prompt_proof(self) -> SessionProof:
        marker = self.authority.mint(
            self.delivery,
            issued_at="2026-07-18T20:59:00Z",
            expires_at="2026-07-18T21:01:00Z",
        )
        return SessionProof(
            session_id="session-proof",
            marker=marker,
            source_events=(
                SourceEventRef(
                    SOURCE,
                    "boot-vesper",
                    10,
                    "cmux-input-10",
                    "surface.input_sent",
                ),
                SourceEventRef(
                    SOURCE,
                    "boot-vesper",
                    11,
                    "cmux-prompt-11",
                    "workspace.prompt.submitted",
                    binding_id=self.delivery.binding_id,
                    marker_signature=marker.signature,
                ),
                SourceEventRef(
                    SOURCE,
                    "boot-vesper",
                    12,
                    "cmux-hook-12",
                    "agent.hook.UserPromptSubmit",
                    session_id="session-proof",
                ),
            ),
            turn_id="turn-proof",
        )

    def completed_proof(self, prompt: SessionProof) -> SessionProof:
        return replace(
            prompt,
            source_events=(
                *prompt.source_events,
                SourceEventRef(
                    SOURCE,
                    "boot-vesper",
                    13,
                    "cmux-stop-13",
                    "agent.hook.Stop",
                    session_id="session-proof",
                ),
            ),
        )

    def receipt(
        self,
        receipt_id: str,
        evidence: EvidenceClass,
        *,
        proof: SessionProof | None = None,
        source_event_id: str | None = None,
    ) -> Receipt:
        return Receipt(
            receipt_id=receipt_id,
            event_id=self.delivery.event_id,
            delivery_id=self.delivery.delivery_id,
            attempt_id=self.delivery.attempt_id,
            binding_id=self.delivery.binding_id,
            evidence_provider_id="cmux-provider",
            evidence_class=evidence,
            proof_method=METHOD,
            observed_at=NOW,
            source_event_id=source_event_id,
            proof=proof,
        )

    def prompt_receipt(self, proof: SessionProof | None = None) -> Receipt:
        proof = proof or self.prompt_proof()
        return self.receipt(
            "receipt-prompt",
            EvidenceClass.HARNESS_PROMPT_ACCEPTED,
            proof=proof,
            source_event_id=proof.source_events[-1].source_event_id,
        )


@pytest.fixture
def case():
    current = ProofCase()
    try:
        yield current
    finally:
        current.close()


def test_full_prompt_and_matching_stop_chain_advance(case: ProofCase) -> None:
    prompt_receipt = case.prompt_receipt()
    prompt = case.reducer.submit(prompt_receipt)
    completed_proof = case.completed_proof(prompt_receipt.proof)
    completed = case.reducer.submit(
        case.receipt(
            "receipt-completed",
            EvidenceClass.HARNESS_TURN_COMPLETED,
            proof=completed_proof,
            source_event_id=completed_proof.source_events[-1].source_event_id,
        )
    )

    observations = {
        "prompt_state": prompt.state.value,
        "completed_state": completed.state.value,
        "disposition": completed.disposition.value,
        "accepted_audit_classes": [
            row["evidence_class"]
            for row in case.store.audit_for(case.delivery.delivery_id)
        ],
    }
    assert observations == {
        "prompt_state": "in-session",
        "completed_state": "processed",
        "disposition": "completed",
        "accepted_audit_classes": [
            "transport.submit_ack",
            "harness.prompt_accepted",
            "harness.turn_completed",
        ],
    }


def test_session_id_alone_is_rejected_and_audited(case: ProofCase) -> None:
    """Reopen SEV-1 if a matching session ID can replace the proof bundle."""
    result = case.reducer.submit(
        case.receipt(
            "session-only",
            EvidenceClass.HARNESS_PROMPT_ACCEPTED,
            source_event_id="cmux-hook-12",
        )
    )

    assert (result.status, result.reason) == ("rejected", "session_proof_required")
    assert case.store.get_delivery(case.delivery.delivery_id).state is (
        DeliveryState.TRANSPORT_SUBMITTED
    )
    assert case.store.rejections_for(case.delivery.delivery_id)[-1]["reason"] == (
        "session_proof_required"
    )


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        (
            lambda proof: replace(proof, session_id="other-session"),
            "authoritative_session_mismatch",
        ),
        (
            lambda proof: replace(
                proof,
                marker=replace(proof.marker, signature="0" * 64),
            ),
            "marker_signature_invalid",
        ),
        (
            lambda proof: replace(
                proof,
                marker=replace(
                    proof.marker,
                    issued_at="2026-07-18T19:00:00Z",
                    expires_at="2026-07-18T20:00:00Z",
                ),
            ),
            "marker_signature_invalid",
        ),
        (
            lambda proof: replace(
                proof,
                source_events=(
                    proof.source_events[1],
                    proof.source_events[0],
                    proof.source_events[2],
                ),
            ),
            "source_event_chain_wrong_shape",
        ),
        (
            lambda proof: replace(
                proof,
                source_events=(
                    replace(
                        proof.source_events[0], source_instance_id="other-resident"
                    ),
                    *proof.source_events[1:],
                ),
            ),
            "source_instance_mismatch",
        ),
        (
            lambda proof: replace(
                proof,
                source_events=(
                    proof.source_events[0],
                    replace(proof.source_events[1], binding_id="other-binding"),
                    proof.source_events[2],
                ),
            ),
            "prompt_marker_binding_mismatch",
        ),
        (
            lambda proof: replace(
                proof,
                source_events=(
                    replace(proof.source_events[0], boot_id=""),
                    *proof.source_events[1:],
                ),
            ),
            "source_event_identity_invalid",
        ),
    ],
)
def test_forged_or_uncorrelated_prompt_is_rejected(
    case: ProofCase, mutation, reason: str
) -> None:
    """Reopen SEV-1 if any incomplete or forged bundle advances state."""
    proof = mutation(case.prompt_proof())
    result = case.reducer.submit(case.prompt_receipt(proof))

    assert (result.status, result.reason) == ("rejected", reason)
    assert case.store.get_delivery(case.delivery.delivery_id).state is (
        DeliveryState.TRANSPORT_SUBMITTED
    )


def test_expired_but_correctly_signed_marker_is_rejected(case: ProofCase) -> None:
    expired = case.authority.mint(
        case.delivery,
        issued_at="2026-07-18T20:00:00Z",
        expires_at="2026-07-18T20:02:00Z",
    )
    proof = case.prompt_proof()
    proof = replace(
        proof,
        marker=expired,
        source_events=(
            proof.source_events[0],
            replace(proof.source_events[1], marker_signature=expired.signature),
            proof.source_events[2],
        ),
    )

    result = case.reducer.submit(case.prompt_receipt(proof))
    assert result.reason == "marker_expired_or_not_yet_valid"


def test_marker_authority_refuses_overlong_lifetime(case: ProofCase) -> None:
    with pytest.raises(ValueError, match="configured maximum"):
        case.authority.mint(
            case.delivery,
            issued_at="2026-07-18T20:00:00Z",
            expires_at="2026-07-18T21:00:00Z",
        )


def test_marker_token_round_trips_without_becoming_authorization(
    case: ProofCase,
) -> None:
    marker = case.prompt_proof().marker
    decoded = case.authority.decode(case.authority.encode(marker))

    assert decoded == marker
    assert case.authority.validate(decoded, case.delivery, observed_at=NOW) is None


@pytest.mark.parametrize("token", ["", "not-yatagarasu", "ygr1.!", "ygr1.e30"])
def test_malformed_marker_token_has_a_named_failure(
    case: ProofCase, token: str
) -> None:
    with pytest.raises(MarkerError):
        case.authority.decode(token)


def test_malformed_proof_object_is_rejected_instead_of_escaping(
    case: ProofCase,
) -> None:
    malformed = replace(case.prompt_receipt(), proof="not-a-proof")  # type: ignore[arg-type]

    result = case.reducer.submit(malformed)

    assert (result.status, result.reason) == (
        "rejected",
        "session_proof_shape_invalid",
    )
    assert case.store.rejections_for(case.delivery.delivery_id)[-1]["reason"] == (
        "session_proof_shape_invalid"
    )


def test_malformed_proof_on_transport_receipt_is_rejected_before_storage() -> None:
    store = CoreStore()
    try:
        delivery = Delivery(
            event_id="event-transport-shape",
            delivery_id="delivery-transport-shape",
            attempt_id="attempt-transport-shape",
            binding_id="binding-transport-shape",
            recipient_id="yua",
            delivery_mode=DeliveryMode.SESSION_BOUND,
        )
        store.add_delivery(delivery)
        store.set_dispatching(delivery.delivery_id)
        store.register_provider(
            "transport-shape-provider",
            ProviderKind.SESSION_TRANSPORT,
            {EvidenceClass.TRANSPORT_SUBMIT_ACK},
        )
        malformed = Receipt(
            receipt_id="receipt-transport-shape",
            event_id=delivery.event_id,
            delivery_id=delivery.delivery_id,
            attempt_id=delivery.attempt_id,
            binding_id=delivery.binding_id,
            evidence_provider_id="transport-shape-provider",
            evidence_class=EvidenceClass.TRANSPORT_SUBMIT_ACK,
            proof_method="cmux.transport",
            observed_at=NOW,
            proof="not-a-proof",  # type: ignore[arg-type]
        )

        result = ReceiptReducer(store).submit(malformed)

        assert (result.status, result.reason) == (
            "rejected",
            "session_proof_shape_invalid",
        )
        assert store.get_delivery(delivery.delivery_id).state is (
            DeliveryState.DISPATCHING
        )
    finally:
        store.close()


def test_revoked_binding_cannot_advance(case: ProofCase) -> None:
    case.store.revoke_session_binding(case.delivery.binding_id)
    result = case.reducer.submit(case.prompt_receipt())

    assert result.reason == "binding_not_active"
    assert case.store.get_delivery(case.delivery.delivery_id).state is (
        DeliveryState.TRANSPORT_SUBMITTED
    )


def test_different_provider_cannot_submit_for_the_binding(case: ProofCase) -> None:
    case.store.register_provider(
        "other-session-provider",
        ProviderKind.SESSION_TRANSPORT,
        {EvidenceClass.HARNESS_PROMPT_ACCEPTED},
    )
    forged = replace(
        case.prompt_receipt(),
        receipt_id="other-provider-receipt",
        evidence_provider_id="other-session-provider",
    )

    result = case.reducer.submit(forged)
    assert result.reason == "provider_not_authorized_for_binding"


def test_second_active_binding_fails_closed_and_supersession_is_atomic(
    case: ProofCase,
) -> None:
    replacement = case.make_binding(
        binding_id="binding-replacement", session_id="session-replacement"
    )
    with pytest.raises(BindingConflictError):
        case.store.register_session_binding(replacement)

    case.store.supersede_session_binding(case.binding.binding_id, replacement)
    assert case.store.session_binding(case.binding.binding_id)["state"] == "superseded"
    assert case.store.session_binding(replacement.binding_id)["state"] == "active"


def test_invalid_replacement_leaves_old_binding_active(case: ProofCase) -> None:
    invalid = replace(
        case.make_binding(binding_id="binding-invalid", session_id="session-invalid"),
        expires_at="2026-07-18T19:00:00Z",
    )

    with pytest.raises(ValueError, match="expiry"):
        case.store.supersede_session_binding(case.binding.binding_id, invalid)
    assert case.store.session_binding(case.binding.binding_id)["state"] == "active"


def test_binding_cannot_declare_evidence_its_provider_does_not(
    case: ProofCase,
) -> None:
    unsupported = replace(
        case.make_binding(
            binding_id="binding-unsupported",
            recipient_id="tama",
            session_id="session-unsupported",
        ),
        proof_methods=(
            replace(
                case.binding.proof_methods[0],
                evidence_classes=frozenset({EvidenceClass.PARTICIPANT_REPLY_AUTHORED}),
            ),
        ),
    )

    with pytest.raises(ValueError, match="not declared by provider"):
        case.store.register_session_binding(unsupported)


def test_authentic_marker_copied_from_another_delivery_is_rejected(
    case: ProofCase,
) -> None:
    copied_from = replace(
        case.delivery,
        event_id="event-other",
        delivery_id="delivery-other",
        attempt_id="attempt-other",
        binding_id="binding-other",
        recipient_id="tama",
    )
    copied_marker = case.authority.mint(
        copied_from,
        issued_at="2026-07-18T20:59:00Z",
        expires_at="2026-07-18T21:01:00Z",
    )
    proof = case.prompt_proof()
    proof = replace(
        proof,
        marker=copied_marker,
        source_events=(
            proof.source_events[0],
            replace(proof.source_events[1], marker_signature=copied_marker.signature),
            proof.source_events[2],
        ),
    )

    result = case.reducer.submit(case.prompt_receipt(proof))
    assert result.reason == "marker_fields_mismatch"


def test_stop_must_close_the_exact_accepted_prompt(case: ProofCase) -> None:
    accepted = case.prompt_receipt()
    assert case.reducer.submit(accepted).state is DeliveryState.IN_SESSION

    different_prompt = case.prompt_proof()
    different_prompt = replace(
        different_prompt,
        source_events=(
            replace(different_prompt.source_events[0], source_event_id="other-input"),
            *different_prompt.source_events[1:],
        ),
    )
    completed = case.completed_proof(different_prompt)
    result = case.reducer.submit(
        case.receipt(
            "wrong-stop",
            EvidenceClass.HARNESS_TURN_COMPLETED,
            proof=completed,
            source_event_id=completed.source_events[-1].source_event_id,
        )
    )

    assert result.reason == "turn_end_does_not_close_accepted_prompt"
    assert case.store.get_delivery(case.delivery.delivery_id).state is (
        DeliveryState.IN_SESSION
    )


def test_receipt_storage_contains_metadata_but_no_prompt_content(
    case: ProofCase,
) -> None:
    receipt = case.prompt_receipt()
    assert case.reducer.submit(receipt).state is DeliveryState.IN_SESSION
    row = case.store.receipt_record(receipt.receipt_id)

    assert row["session_id"] == "session-proof"
    assert row["marker_signature"] == receipt.proof.marker.signature
    assert "workspace.prompt.submitted" in row["source_event_chain"]
    assert "secret prompt body" not in " ".join(str(value) for value in row)
