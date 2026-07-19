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

    Implementation notes (Copilot round-2 + round-3 fixes):
    - rglob("*.py") walks every subpackage under plugins/cmux/
      yatagarasu_cmux/ — glob("*.py") would miss future subpackages.
    - tokenize gives a token-type stream; we use the COMMENT and
      STRING token ranges to BLANK those positions out of the source
      (replacing characters with spaces while preserving newlines
      so line numbers stay accurate), then run the regexes against
      the blanked source. The previous attempt applied the regexes
      per-token, which made the scan vacuous: `self._surface`
      tokenizes as three tokens (NAME 'self', OP '.', NAME
      '_surface') and no single token contains a multi-token
      pattern like `self\\s*\\.\\s*_surface`. Blanking preserves the
      patterns' ability to see contiguous text while genuinely
      excluding comment and string content, including multi-line
      docstrings and string literals.

    Acceptance (per Aoi's explicit criterion on the ygr-round1-qa
    CID): a fixture file containing `self._surface` inside a real
    code line must FAIL the scan, and the same string inside a
    docstring must PASS it. Both directions, both seen to behave.
    The `_verify_scan` helper below makes this assertion explicit.
    """
    import io
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

    def _scan_for_violations(
        source_text: str, src_path: Path
    ) -> list[tuple[int, int, str, str]]:
        """Run the banned-pattern regex over a blanked version of `source_text`.

        Returns a list of (line, col, pattern, why) tuples; an empty list
        means no violation was found. The blanking replaces characters that
        fall inside COMMENT or STRING tokens with spaces (preserving
        newlines so line numbers stay accurate), so multi-line docstrings
        and string literals are genuinely excluded while the regex
        patterns still see contiguous text.
        """
        try:
            tokens = list(tokenize.generate_tokens(io.StringIO(source_text).readline))
        except tokenize.TokenError as exc:
            pytest.fail(
                f"{src_path}: tokenize failed ({exc}); the source file is "
                "not valid Python and the static scan cannot proceed."
            )

        # Build a list of (start_offset, end_offset, line) for each
        # COMMENT or STRING token. We use this to blank those ranges
        # out of the source so the regexes see contiguous text but
        # cannot match inside comments or strings.
        masked_ranges: list[tuple[int, int, int]] = []
        for tok_type, _tok_string, (srow, scol), (erow, ecol), _ in tokens:
            if tok_type not in (tokenize.COMMENT, tokenize.STRING):
                continue
            # Convert (srow, scol) and (erow, ecol) to absolute offsets.
            # tokenize uses 1-indexed rows and 0-indexed cols.
            start_offset = (
                sum(len(line) + 1 for line in source_text.splitlines()[: srow - 1])
                + scol
            )
            end_offset = (
                sum(len(line) + 1 for line in source_text.splitlines()[: erow - 1])
                + ecol
            )
            masked_ranges.append((start_offset, end_offset, srow))

        # Build the blanked source: replace masked characters with
        # spaces, but keep newlines so line numbers are preserved.
        chars = list(source_text)
        for start, end, _line in masked_ranges:
            for i in range(start, min(end, len(chars))):
                if chars[i] != "\n":
                    chars[i] = " "
        blanked = "".join(chars)

        violations: list[tuple[int, int, str, str]] = []
        for match in banned_re.finditer(blanked):
            idx = next(i for i in range(1, len(banned_patterns) + 1) if match.group(i))
            # Compute the original line number from the offset of
            # the match start. The blanked source has the same
            # newlines as the original, so line numbers are stable.
            line = blanked.count("\n", 0, match.start()) + 1
            violations.append(
                (
                    line,
                    match.start(),
                    banned_patterns[idx - 1][0],
                    reason_by_index[idx - 1],
                )
            )
        return violations

    # Walk every Python file under the plugin source, recursively.
    for src_path in sorted(src_root.rglob("*.py")):
        # Skip __pycache__ build artifacts.
        if "__pycache__" in src_path.parts:
            continue
        source_text = src_path.read_text(encoding="utf-8")
        for line, _offset, pattern, why in _scan_for_violations(source_text, src_path):
            pytest.fail(
                f"{src_path.name}:{line}: banned surface-id caching pattern. "
                f"Pattern: {pattern!r}; Why: {why}. "
                "The plugin must address by identity on every send."
            )

    # Acceptance: a fixture file containing `self._surface` inside a
    # real code line must FAIL the scan, and the same string inside a
    # docstring must PASS it. Both directions, both seen to behave.
    # The fixture lives at /tmp at test time so it never lands in
    # the actual plugin source tree.
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        bad_path = Path(tmp) / "bad_under_test.py"
        bad_path.write_text(
            "class X:\n    def f(self):\n        return self._surface\n",
            encoding="utf-8",
        )
        bad_violations = _scan_for_violations(
            bad_path.read_text(encoding="utf-8"), bad_path
        )
        assert bad_violations, (
            "detector regression: a file with `self._surface` in real code "
            "must produce at least one violation; the scan is vacuous"
        )

        ok_path = Path(tmp) / "ok_under_test.py"
        ok_path.write_text(
            "class X:\n"
            '    """A docstring that contains `self._surface` as text only."""\n'
            '    surface_name = "self._surface"  # as a string literal too.\n'
            "    def f(self):\n"
            "        return 1\n",
            encoding="utf-8",
        )
        ok_violations = _scan_for_violations(
            ok_path.read_text(encoding="utf-8"), ok_path
        )
        assert not ok_violations, (
            "detector regression: `self._surface` only inside docstrings or "
            "string literals must not produce a violation; the scan is "
            f"rejecting legitimate code. Got: {ok_violations!r}"
        )
