"""Live preview-budget canary for the injector-to-projector marker seam."""

from __future__ import annotations

from yatagarasu_cmux import Injector
from yatagarasu_cmux.harness_profiles import HarnessKind
from yatagarasu_cmux.marker import extract
from yatagarasu_cmux.outcome import SubmitOutcome

from yatagarasu_core import Delivery, DeliveryMode
from yatagarasu_core.proofs import MarkerAuthority

ISSUED_AT = "2026-07-19T21:00:00Z"
EXPIRES_AT = "2026-07-19T21:05:00Z"


class _Resolver:
    def resolve(self, identity: str) -> str:
        return "surface:canary"


class _Transport:
    def __init__(self) -> None:
        self.text = ""

    def send_text(self, surface: str, text: str) -> None:
        self.text = text

    def submit(self, surface: str, key: str) -> None:
        pass


class _Observer:
    def observe(self, marker, timeout_s):
        yield "surface.input_sent"
        yield "workspace.prompt.submitted"


def test_injected_marker_survives_the_240_character_live_preview() -> None:
    """SEV-1 reopen: the marker tail is truncated before its signature."""
    authority = MarkerAuthority(b"canary-signing-key")
    delivery = Delivery(
        event_id="ev-canary",
        delivery_id="d" * 64,
        attempt_id="a-canary",
        binding_id="b" * 64,
        recipient_id="recipient-canary",
        delivery_mode=DeliveryMode.SESSION_BOUND,
    )
    transport = _Transport()
    injector = Injector(
        resolver=_Resolver(),
        transport=transport,
        observer=_Observer(),
        marker_authority=authority,
    )

    result = injector.deliver(
        "recipient-canary",
        delivery,
        "body follows the marker",
        ISSUED_AT,
        EXPIRES_AT,
        harness=HarnessKind.CODEX,
    )

    assert result.outcome is SubmitOutcome.SUBMITTED
    token = transport.text.split(" ", 1)[0]
    assert token.startswith("ygr1s.")
    assert len(token) <= 180

    live_preview = (
        transport.text[:239] + "…" if len(transport.text) > 240 else transport.text
    )
    observed = extract(None, live_preview)
    assert observed is not None
    assert observed.delivery_id == delivery.delivery_id
    assert observed.binding_id == delivery.binding_id
    authoritative = authority.mint(delivery, issued_at=ISSUED_AT, expires_at=EXPIRES_AT)
    assert observed.signature == authoritative.signature
