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
        "key": "enter",
        "methods": ["surface.send_text", "surface.send_key"],
    }


def test_codex_next_turn_is_plain_text_then_tab(tmp_path) -> None:
    observations = _exercise_profile(tmp_path, HarnessKind.CODEX)

    assert observations == {
        "text": "signed envelope",
        "key": "tab",
        "methods": ["surface.send_text", "surface.send_key"],
    }


def test_hermes_next_turn_is_queue_command_then_enter(tmp_path) -> None:
    observations = _exercise_profile(tmp_path, HarnessKind.HERMES)

    assert observations == {
        "text": "/queue signed envelope",
        "key": "enter",
        "methods": ["surface.send_text", "surface.send_key"],
    }


def test_transport_never_uses_focus_read_or_admission_rpc(tmp_path) -> None:
    observations = _exercise_profile(tmp_path, HarnessKind.CODEX)

    assert observations["methods"] == ["surface.send_text", "surface.send_key"]


def _exercise_profile(tmp_path, kind: HarnessKind) -> dict[str, object]:
    socket_path = short_socket_path(tmp_path, f"profile-{kind.value}")
    with CmuxSocketHarness(socket_path, []) as harness:
        transport = CmuxSocketTransport.from_socket_path(socket_path)
        profile = profile_for(kind)
        transport.send_text(
            "00000000-0000-0000-0000-000000000026", profile.render("signed envelope")
        )
        transport.submit("00000000-0000-0000-0000-000000000026", profile.submit_key)

    requests = harness.command_requests
    return {
        "text": requests[0]["params"]["text"],
        "key": requests[1]["params"]["key"],
        "methods": [request["method"] for request in requests],
    }
