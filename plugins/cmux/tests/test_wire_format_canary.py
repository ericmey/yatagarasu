"""The canary for #47: does the string we WRITE match the regex we READ?

This assertion is the whole seam audit compressed into one line, and it is the
test that would have caught the dead receipt path on day one. #47's scope named
it explicitly. It was the one item nobody built, and the path stayed dead for a
day while every suite reported green.

Everything else about the marker path was covered, which is exactly why the gap
survived three reviewers. The injector's tests exercised the injector. The
emitter's tests fed it ``ygr1.`` tokens built by hand. The forgery regression test
builds its markers with ``MarkerAuthority.encode`` and hands them straight to the
emitter. Every one of those passed while the two ends spoke different languages,
because not one of them put the **producer's real output** in front of the
**consumer's real matcher**.

Per Tama's literal-artifact criterion (#48): the artifact one side emits, fed to
the matcher the other side uses, with no reconstruction in between.
"""

from __future__ import annotations

from yatagarasu_cmux.receipt_emitter import _YGR1_RE

from yatagarasu_core import Delivery, DeliveryMode
from yatagarasu_core.proofs import MarkerAuthority

ISSUED_AT = "2026-07-19T21:00:00Z"
EXPIRES_AT = "2026-07-19T21:05:00Z"


def test_the_injected_marker_is_matched_by_the_emitter_regex() -> None:
    """Producer's literal output vs consumer's literal matcher.

    ``MarkerAuthority.encode`` is what the injector embeds (``injector.py:130``:
    ``text = f"{encoded_marker} {body}"``) and ``_YGR1_RE`` is what the emitter
    searches prompt previews with. Nothing here is constructed to fit.

    If this fails, no prompt marker is ever recognised, no chain is built, no
    Stop emits a receipt, and every suite stays green while the receipt path is
    dead in production. That is not hypothetical — it is what shipped, and what
    this test now prevents from shipping again.
    """
    authority = MarkerAuthority(b"canary-signing-key")
    delivery = Delivery(
        event_id="ev-canary",
        delivery_id="d-canary",
        attempt_id="a-canary",
        binding_id="b-canary",
        recipient_id="recipient-canary",
        delivery_mode=DeliveryMode.SESSION_BOUND,
    )

    embedded = authority.encode(
        authority.mint(delivery, issued_at=ISSUED_AT, expires_at=EXPIRES_AT)
    )

    match = _YGR1_RE.search(f"{embedded} some message body")

    assert match is not None, (
        f"the injector embeds {embedded!r} and the emitter searches for"
        f" {_YGR1_RE.pattern!r} — these do not match, so the production receipt"
        " path is dead"
    )
    assert match.group(1) == embedded, (
        "the regex matched but did not capture the whole token, so the emitter"
        " would decode a truncated marker"
    )
