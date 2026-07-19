"""The literal CMUX event capture from ygr-round1-e2e-20260719, as fixtures.

The capture file `core/tests/ygr_round1_e2e_20260719_capture.jsonl`
is the verbatim 8-event sequence Yua captured from a live disposable Codex
seat on 2026-07-19. It is preserved here as JSONL so future tests can
read it byte-for-byte without re-deriving anything by hand.

What the capture contains:

  - 1 surface.input_sent (the user typed into the surface)
  - 3 workspace.prompt.submitted (cmux submitted the prompt three times,
    all carrying the SAME marker)
  - 3 agent.hook.UserPromptSubmit (the agent received it three times)
  - 1 agent.hook.Stop (the agent finished)

Real values preserved (these are the actual bytes from Yua's capture):

  - boot_id=93651E9F-81D7-45F7-B146-96786D1A88E0
  - source_instance_id (cmux-resident): surface:10 / workspace:10
  - surface_id=7657E508-6834-4701-94B2-1B8B9D9EF6D7 (on input_sent only)
  - workspace_id=776E9D1D-930B-4822-8FA0-ADCCCF5CA620
  - session_id=codex-019f7b28-a5e8-77c3-9f30-3229745ecf1c (on hook events only)
  - marker field on prompt.submitted: ygr1.eyJhdH...QxNj (BOUNDED — CMUX
    exposes only a prefix-and-suffix preview, not the full marker token)

Why this fixture exists:

  Yua's capture exposed that core requires an exact 4-event chain but
  live cmux emits 8 for one turn. The dedup-to-first normalizer (Yua's
  work on #46 and #59) is what collapses 8 -> 4. This fixture is what
  the normalizer is tested against: real seq numbers, real null
  session_ids on workspace.prompt.submitted, real null surface_ids on
  the hook events. A hand-built chain in a test would re-introduce
  the unobserved assumption that core's contract was about.

Synthetic-but-consistent metadata:

  The bounded marker preview cannot be decoded by MarkerAuthority.decode;
  the validator's signature check requires the full token. This module
  re-mints a marker using the SAME authority that Yua's capture came
  from, with delivery_id / event_id / attempt_id / binding_id consistent
  with the capture's workspace context. The marker is therefore
  syntactically valid against the validator while still driving the
  test from the literal 8-event capture (no hand-built chain).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from yatagarasu_core import (
    CorrelationRule,
    Delivery,
    DeliveryMode,
    EvidenceClass,
    MarkerAuthority,
    ProofMethodRegistration,
    SessionBinding,
    SessionProof,
    SourceEventRef,
    SourceKind,
)

FIXTURE_PATH = Path(__file__).resolve().parent / "ygr_round1_e2e_20260719_capture.jsonl"

#: Constants from Yua's live capture (literal where the capture carries them;
#: synthetic-but-consistent where the bounded marker requires construction).
BOOT_ID = "93651E9F-81D7-45F7-B146-96786D1A88E0"
SOURCE_INSTANCE = "cmux-resident-vesper-ygr-round1-e2e"
WORKSPACE_ID = "776E9D1D-930B-4822-8FA0-ADCCCF5CA620"
SURFACE_ID = "7657E508-6834-4701-94B2-1B8B9D9EF6D7"
SESSION_ID = "codex-019f7b28-a5e8-77c3-9f30-3229745ecf1c"
ISSUED_AT = "2026-07-19T16:17:14.665699Z"
EXPIRES_AT = "2026-07-19T16:21:14.665699Z"
METHOD = "cmux.event_bus.harness_hook_relay"
PROVIDER_ID = "cmux-provider-ygr-round1-e2e"

#: Synthetic delivery identifiers consistent with the workspace context.
#: These are not in the capture (cmux-exposed events don't carry the
#: delivery_id); they are reconstructed from the capture's workspace
#: and the marker minting context that Yua captured separately.
DELIVERY_ID = "delivery-ygr-round1-e2e-canary"
EVENT_ID = "event-ygr-round1-e2e-canary"
ATTEMPT_ID = "attempt-ygr-round1-e2e-canary"
BINDING_ID = "binding-ygr-round1-e2e-canary"


def load_raw_events(path: Path = FIXTURE_PATH) -> list[dict[str, Any]]:
    """Load the literal 8-event capture from the JSONL fixture.

    Returns the events in capture order, with seq numbers preserved
    exactly. The marker field is the bounded preview as exposed by CMUX;
    it is NOT a parseable token.
    """
    events: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            events.append(json.loads(line))
    return events


def make_marker(authority: MarkerAuthority) -> Any:
    """Mint a marker consistent with the capture's context.

    The marker carries the four-key contract (event_id, delivery_id,
    attempt_id, binding_id, authority_scope) required by
    validate_session_proof. The signature is the HMAC over those fields
    under the given authority's signing key.
    """
    delivery = Delivery(
        event_id=EVENT_ID,
        delivery_id=DELIVERY_ID,
        attempt_id=ATTEMPT_ID,
        binding_id=BINDING_ID,
        recipient_id="codex-recipient",
        delivery_mode=DeliveryMode.SESSION_BOUND,
    )
    return authority.mint(delivery, issued_at=ISSUED_AT, expires_at=EXPIRES_AT)


def make_registration() -> ProofMethodRegistration:
    """Build the proof-method registration that the validator cross-checks.

    The session-proof evidence classes are the ones that
    validate_session_proof expects; the source_instance_id matches
    the SOURCE_INSTANCE constant so the validator's
    source_instance_mismatch check does not fire.
    """
    return ProofMethodRegistration(
        proof_method=METHOD,
        source_kind=SourceKind.EVENT_BUS,
        source_instance_id=SOURCE_INSTANCE,
        correlation_rule=CorrelationRule.CMUX_HARNESS_CHAIN,
        evidence_classes=frozenset(
            {
                EvidenceClass.HARNESS_PROMPT_ACCEPTED,
                EvidenceClass.HARNESS_TURN_STARTED,
                EvidenceClass.HARNESS_TURN_COMPLETED,
            }
        ),
    )


def make_binding() -> SessionBinding:
    """Build a session binding consistent with the capture's workspace context.

    Yua's capture did not include the binding in the event payload, but
    a session proof requires it for the marker_authority.validate call
    inside validate_session_proof. The binding is reconstructed with
    fields consistent with the workspace_id / session_id from the capture.
    """
    return SessionBinding(
        binding_id=BINDING_ID,
        recipient_id="codex-recipient",
        provider_id=PROVIDER_ID,
        adapter_instance_id="cmux-resident-vesper",
        harness="codex",
        session_id=SESSION_ID,
        established_at="2026-07-19T16:00:00Z",
        expires_at="2026-07-19T18:00:00Z",
        proof_methods=(make_registration(),),
    )


def dedup_to_first(
    raw_events: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Apply the dedup-to-first normalizer to the raw capture.

    Production path: Yua's #46 implements this. For testing without
    the normalizer in place, the test loads the deduped form directly
    from the deduped_events static list below.
    """
    seen_event_names: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for event in raw_events:
        name = event["name"]
        if name in seen_event_names and name in (
            "workspace.prompt.submitted",
            "agent.hook.UserPromptSubmit",
        ):
            continue
        seen_event_names.add(name)
        deduped.append(event)
    return deduped


def build_deduped_proof(
    authority: MarkerAuthority,
    *,
    tamper_marker_signature: str | None = None,
    drop_stop_event: bool = False,
    swap_event_order: bool = False,
) -> tuple[SessionProof, Delivery]:
    """Build a SessionProof from the deduped chain with the marker attached.

    The proof's source_events are the deduped form of the literal
    8-event capture: input_sent + first prompt.submitted + first
    UserPromptSubmit + Stop. Tampering the marker_signature is the
    red-proof case; dropping Stop or swapping order exercises other
    rejection paths.
    """
    raw_events = load_raw_events()
    deduped = dedup_to_first(raw_events)
    if drop_stop_event:
        deduped = [e for e in deduped if e["name"] != "agent.hook.Stop"]
    if swap_event_order:
        deduped = list(reversed(deduped))

    marker = make_marker(authority)
    delivery = Delivery(
        event_id=EVENT_ID,
        delivery_id=DELIVERY_ID,
        attempt_id=ATTEMPT_ID,
        binding_id=BINDING_ID,
        recipient_id="codex-recipient",
        delivery_mode=DeliveryMode.SESSION_BOUND,
    )
    marker_sig = tamper_marker_signature or marker.signature

    source_events: list[SourceEventRef] = []
    for event in deduped:
        is_prompt = event["name"] == "workspace.prompt.submitted"
        source_events.append(
            SourceEventRef(
                source_instance_id=SOURCE_INSTANCE,
                boot_id=BOOT_ID,
                seq=event["seq"],
                source_event_id=event["id"],
                event_name=event["name"],
                session_id=event["session_id"],
                binding_id=BINDING_ID if is_prompt else None,
                marker_signature=marker_sig if is_prompt else None,
            )
        )

    return (
        SessionProof(
            session_id=SESSION_ID,
            marker=marker,
            source_events=tuple(source_events),
            turn_id="turn-ygr-round1-e2e",
        ),
        delivery,
    )


def iter_raw_events(path: Path = FIXTURE_PATH) -> Iterator[dict[str, Any]]:
    """Iterator over the literal 8-event capture, one event at a time.

    Provided so tests can assert properties of the raw capture
    (event count, sequence shape, null-field distribution) without
    loading the entire list into memory.
    """
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            yield json.loads(line)
