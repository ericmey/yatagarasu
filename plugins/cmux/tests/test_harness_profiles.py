"""Behavioral contract for harness-specific next-turn submission.

Reopen SEV-1 if a delivery uses the harness's steer or interrupt path instead
of its explicit next-turn path.  A locally successful key press is not enough:
the text and key must be the exact pair the selected harness interprets as a
queued follow-up.
"""

from __future__ import annotations

from yatagarasu_cmux.harness_profiles import HarnessKind, profile_for
from yatagarasu_cmux.socket_transport import CmuxSocketTransport

from .socket_harness import CmuxSocketHarness, short_socket_path


def test_claude_code_next_turn_is_plain_text_then_enter(tmp_path) -> None:
    observations = _exercise_profile(tmp_path, HarnessKind.CLAUDE_CODE)

    assert observations == {
        "text": "signed envelope",
        "keys": ["enter"],
        "methods": ["surface.send_text", "surface.send_key"],
    }


def test_codex_next_turn_uses_state_independent_tab_then_enter(tmp_path) -> None:
    observations = _exercise_profile(tmp_path, HarnessKind.CODEX)

    assert observations == {
        "text": "signed envelope",
        "keys": ["tab", "enter"],
        "methods": ["surface.send_text", "surface.send_key", "surface.send_key"],
    }


def test_hermes_next_turn_is_queue_command_then_enter(tmp_path) -> None:
    observations = _exercise_profile(tmp_path, HarnessKind.HERMES)

    assert observations == {
        "text": "/queue signed envelope",
        "keys": ["enter"],
        "methods": ["surface.send_text", "surface.send_key"],
    }


def test_transport_never_uses_focus_read_or_admission_rpc(tmp_path) -> None:
    observations = _exercise_profile(tmp_path, HarnessKind.CODEX)

    assert observations["methods"] == [
        "surface.send_text",
        "surface.send_key",
        "surface.send_key",
    ]


def test_codex_sequence_submits_once_when_idle_and_queues_once_when_busy() -> None:
    """Model the two live canaries; reopen if Enter can steer a busy turn."""
    keys = profile_for(HarnessKind.CODEX).submit_keys

    idle = _CodexState(busy=False)
    busy = _CodexState(busy=True)
    for key in keys:
        idle.apply(key)
        busy.apply(key)

    assert (idle.submitted, idle.queued, idle.steered) == (1, 0, 0)
    assert (busy.submitted, busy.queued, busy.steered) == (0, 1, 0)


class _CodexState:
    """The observed Codex 0.144.5 composer semantics from the live canary."""

    def __init__(self, *, busy: bool) -> None:
        self.busy = busy
        self.composer_has_text = True
        self.submitted = 0
        self.queued = 0
        self.steered = 0

    def apply(self, key: str) -> None:
        if key == "tab" and self.busy and self.composer_has_text:
            self.queued += 1
            self.composer_has_text = False
        elif key == "enter" and self.composer_has_text:
            if self.busy:
                self.steered += 1
            else:
                self.submitted += 1
            self.composer_has_text = False


def _exercise_profile(tmp_path, kind: HarnessKind) -> dict[str, object]:
    socket_path = short_socket_path(tmp_path, f"profile-{kind.value}")
    with CmuxSocketHarness(socket_path, []) as harness:
        transport = CmuxSocketTransport.from_socket_path(socket_path)
        profile = profile_for(kind)
        transport.send_text(
            "00000000-0000-0000-0000-000000000026", profile.render("signed envelope")
        )
        for key in profile.submit_keys:
            transport.submit("00000000-0000-0000-0000-000000000026", key)

    requests = harness.command_requests
    return {
        "text": requests[0]["params"]["text"],
        "keys": [request["params"]["key"] for request in requests[1:]],
        "methods": [request["method"] for request in requests],
    }
