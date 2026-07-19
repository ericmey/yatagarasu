"""Explicit next-turn submission profiles for supported agent harnesses.

The same terminal input has materially different busy-turn semantics:

* Claude Code queues plain input submitted with Enter.
* Codex treats Enter as a steer and Tab as the explicit next-turn queue action.
* Hermes defaults plain Enter to interrupt, while ``/queue`` is the stable
  non-interrupting next-turn command.

Yatagarasu selects a profile from the authoritative session binding.  It never
infers a harness or its busy state from terminal contents.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class HarnessKind(StrEnum):
    """Harness identity recorded by the authoritative session binding."""

    CLAUDE_CODE = "claude-code"
    CODEX = "codex"
    HERMES = "hermes"


class BusyEnterBehavior(StrEnum):
    """What plain Enter means while the selected harness is working."""

    QUEUE = "queue"
    STEER = "steer"
    INTERRUPT = "interrupt"


@dataclass(frozen=True, slots=True)
class HarnessProfile:
    """The exact text/key pair that requests a non-interrupting next turn.

    The conditions under which a profile is proven must be the conditions
    under which it runs, so load-bearing timing belongs in this production
    contract rather than only in a canary or test harness.
    """

    kind: HarnessKind
    submit_keys: tuple[str, ...]
    busy_enter_behavior: BusyEnterBehavior
    text_prefix: str = ""
    inter_key_delay_s: float = 0.0

    def render(self, envelope: str) -> str:
        """Render one signed envelope without inspecting runtime UI state."""
        if not envelope:
            raise ValueError("cannot submit an empty envelope")
        return f"{self.text_prefix}{envelope}"


_PROFILES = {
    HarnessKind.CLAUDE_CODE: HarnessProfile(
        kind=HarnessKind.CLAUDE_CODE,
        submit_keys=("enter",),
        busy_enter_behavior=BusyEnterBehavior.QUEUE,
    ),
    HarnessKind.CODEX: HarnessProfile(
        kind=HarnessKind.CODEX,
        # Live Codex 0.144.5 proof: Tab queues while busy but does not submit
        # while idle. Enter submits while idle but steers while busy. Tab then
        # Enter is state-independent: busy Tab consumes the text into the queue
        # and the empty Enter is inert; idle Tab is inert and Enter submits.
        submit_keys=("tab", "enter"),
        busy_enter_behavior=BusyEnterBehavior.STEER,
        inter_key_delay_s=0.1,
    ),
    HarnessKind.HERMES: HarnessProfile(
        kind=HarnessKind.HERMES,
        submit_keys=("enter",),
        busy_enter_behavior=BusyEnterBehavior.INTERRUPT,
        text_prefix="/queue ",
    ),
}


def profile_for(kind: HarnessKind | str) -> HarnessProfile:
    """Return the declared profile or reject an unsupported harness."""
    try:
        resolved = HarnessKind(kind)
    except ValueError as exc:
        raise ValueError(f"unsupported harness: {kind!r}") from exc
    return _PROFILES[resolved]
