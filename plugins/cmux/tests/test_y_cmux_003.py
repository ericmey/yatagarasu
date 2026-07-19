"""Y-CMUX-003 — No cached surface binding; restart re-resolves by identity.

Hard rule. Surface IDs are not stable across session restarts; Eric's
restart incident reproduced live 2026-07-18: Tama's seat moved
`surface:33 → surface:3`. Delivery still succeeded only because the
plugin re-resolves by identity on every send.

This file is the dedicated acceptance hook for issue #17's slice. The
unit-level coverage already exists in `test_injector.py::test_surface_is_resolved_every_send_never_cached`;
this file extends the contract with three more shapes:

  1. RESTART SHAPE: two consecutive delivers, surface changes between
     them (simulating a session restart). Both sends land in the
     resolved surface; the resolver is called twice; the two surface
     IDs differ.
  2. ADVERSARIAL CACHE: force the resolver's local state to suggest a
     stale surface; assert the next deliver still re-resolves
     (the plugin does not consult any cached binding).
  3. STATIC: grep the plugin source for surface-id caching patterns
     that would create a `surface:33 → surface:3`-style silent loss.

Reopen condition (SEV-1): any send ever lands in a stale surface_id
(silent delivery loss to a dead or reassigned pane).
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path

import pytest
from yatagarasu_cmux import (
    EVENT_INPUT_SENT,
    EVENT_PROMPT_SUBMITTED,
    Injector,
    Marker,
    ResolutionError,
)
from yatagarasu_cmux.outcome import SubmitResult

SIGNING_KEY = b"acceptance-only-signing-key"


class _Resolver:
    """Records every resolve so we can prove no handle is cached.

    A `pop(0)` from a queue simulates the host returning a fresh
    surface on each resolve call. After a session restart, the queue
    is replaced with a new surface id, so the next resolve returns
    a different surface even though the identity is unchanged.
    """

    def __init__(self, handles: list[str]) -> None:
        self._handles = list(handles)
        self.calls: list[str] = []

    def resolve(self, identity: str) -> str:
        self.calls.append(identity)
        if not self._handles:
            raise ResolutionError(f"no live surface for {identity}")
        return self._handles.pop(0)


class _Transport:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []
        self.submitted: list[str] = []

    def send_text(self, surface: str, text: str) -> None:
        self.sent.append((surface, text))

    def submit(self, surface: str) -> None:
        self.submitted.append(surface)


class _Observer:
    def __init__(self, events: list[str]) -> None:
        self._events = list(events)

    def observe(self, marker: Marker, timeout_s: float) -> Iterable[str]:
        yield from self._events


def _build_injector(events: list[str], resolver: _Resolver) -> Injector:
    return Injector(
        resolver=resolver,
        transport=_Transport(),
        observer=_Observer(events),
        signing_key=SIGNING_KEY,
        submit_timeout_s=0.05,
    )


def test_y_cmux_003_restart_resolves_to_new_surface_with_same_identity() -> None:
    """Restart shape: two consecutive delivers, surface changes between them.

    Before the restart the resolver returns `surface:33`; after the
    restart the same identity resolves to `surface:3`. Both sends
    land in their resolved surface; the resolver is consulted twice;
    the pre/post surfaces differ.
    """
    resolver = _Resolver(["surface:33", "surface:3"])
    transport = _Transport()
    inj = Injector(
        resolver=resolver,
        transport=transport,
        observer=_Observer([EVENT_INPUT_SENT, EVENT_PROMPT_SUBMITTED]),
        signing_key=SIGNING_KEY,
        submit_timeout_s=0.05,
    )

    # First send: lands in surface:33
    first: SubmitResult = inj.deliver("peer", "d-001", "first body")
    # Second send: lands in surface:3 (the post-restart surface)
    second: SubmitResult = inj.deliver("peer", "d-002", "second body")

    assert first.is_proven and second.is_proven, (
        "both sends must prove submission; this is a positive-control "
        "for the re-resolve-after-restart case."
    )

    # Resolver consulted twice — proves no handle was cached between sends.
    assert resolver.calls == ["peer", "peer"], (
        f"resolver must be called once per send; got calls={resolver.calls!r}. "
        "A cached handle would skip the second resolve."
    )

    # Surface IDs differ — proves the restart actually changed the binding.
    surfaces_used = [s for s, _ in transport.sent]
    assert surfaces_used == ["surface:33", "surface:3"], (
        f"sends must land in the resolved surfaces in order; "
        f"got {surfaces_used!r}. A cached handle would deliver both to surface:33."
    )

    # Identity unchanged — the seat's identity string is stable across restarts.
    # This is the property the predecessor failed: identity is the long-lived key,
    # surface is the ephemeral handle.


def test_y_cmux_003_adversarial_stale_state_does_not_persist_across_sends() -> None:
    """Force the resolver to expose a stale surface state.

    The adversarial fixture is the failure mode that motivated the
    hook: an in-process cache (or anything else that makes the second
    send reuse the first surface) would deliver both sends into the
    same handle. Even with a resolver that exposes fresh surfaces, the
    plugin must NOT cache — every send re-resolves, every send lands
    in the resolved surface.
    """
    resolver = _Resolver(["surface:stale", "surface:fresh"])
    transport = _Transport()
    inj = Injector(
        resolver=resolver,
        transport=transport,
        observer=_Observer([EVENT_INPUT_SENT, EVENT_PROMPT_SUBMITTED]),
        signing_key=SIGNING_KEY,
        submit_timeout_s=0.05,
    )

    inj.deliver("peer", "d-003", "body one")
    inj.deliver("peer", "d-004", "body two")

    surfaces_used = [s for s, _ in transport.sent]
    assert surfaces_used == ["surface:stale", "surface:fresh"], (
        "even with adversarial resolver state, the plugin must use the "
        "freshly-resolved surface on every send. A cached handle would "
        "deliver both to 'surface:stale' — that is the silent-loss case."
    )


def test_y_cmux_003_no_surface_id_cache_in_plugin_source() -> None:
    """Static test: grep the plugin source for surface-id caching.

    Banned patterns: any module-scoped or class-field cache of
    surface_id; any TTL > 0 on a surface-binding map; any in-process
    map keyed on surface IDs that lives longer than one send.

    The plugin must address by identity, never cache the resolved
    handle. Future regressions where someone adds a `self._surface`
    or a `functools.lru_cache` on the resolver will be caught here.

    Implementation notes (Copilot round-2 fixes for this hook):
    - rglob("*.py") walks every subpackage under plugins/cmux/
      yatagarasu_cmux/ — glob("*.py") would miss future subpackages.
    - tokenize.generate_tokens gives a token-type stream; we skip
      tokens inside COMMENT and STRING (including docstrings) at the
      source rather than by inspecting per-line prefix characters.
      The previous "skip lines starting with # or triple quotes"
      check missed banned patterns that appear on a LATER line of a
      multi-line docstring or inside a regular string literal.
    """
    import tokenize

    plugin_root = Path(__file__).resolve().parents[1]  # plugins/cmux
    src_root = plugin_root / "yatagarasu_cmux"

    banned_patterns = [
        (
            r"\bself\s*\.\s*_(?:surface|surface_id|surface_handle)\b",
            "module/instance-level surface cache (banned)",
        ),
        (
            r"\blru_cache\s*\(",
            "lru_cache on the resolver (banned — surface must not be cached)",
        ),
        (
            r"\bttl\s*=\s*[1-9]\d*",
            "TTL > 0 on a binding map (banned — surface is ephemeral)",
        ),
    ]
    banned_re = re.compile("|".join(f"({pat})" for pat, _ in banned_patterns))
    reason_by_index = {i: why for i, (_, why) in enumerate(banned_patterns)}

    # Walk every Python file under the plugin source, recursively, so
    # future subpackages are scanned too.
    for src_path in sorted(src_root.rglob("*.py")):
        # Skip __pycache__ and similar build artifacts that may exist
        # in the source tree after a local run.
        if "__pycache__" in src_path.parts:
            continue
        with src_path.open(encoding="utf-8") as f:
            try:
                tokens = list(tokenize.generate_tokens(f.readline))
            except tokenize.TokenError as exc:
                pytest.fail(
                    f"{src_path}: tokenize failed ({exc}); the source file is "
                    "not valid Python and the static scan cannot proceed."
                )

        # tokenize produces a stream of (token-type, token-string, ...)
        # tuples. COMMENT and STRING tokens are skipped at the source;
        # everything else is checked against the banned patterns.
        for tok_type, tok_string, (srow, _), _, _ in tokens:
            if tok_type in (tokenize.COMMENT, tokenize.STRING):
                continue
            for match in banned_re.finditer(tok_string):
                # Determine which alternative matched (re groups are
                # numbered in declaration order, with the full match in
                # group 0 and each alternative in its own group).
                idx = next(
                    i for i in range(1, len(banned_patterns) + 1) if match.group(i)
                )
                pytest.fail(
                    f"{src_path.name}:{srow}: banned surface-id caching pattern. "
                    f"Pattern: {banned_patterns[idx - 1][0]!r}; "
                    f"Why: {reason_by_index[idx - 1]}. "
                    f"Token: {tok_string!r}. "
                    "The plugin must address by identity on every send."
                )
