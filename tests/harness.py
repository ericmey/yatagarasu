"""Fixture harness for the yatagarasu acceptance suite (issue #5).

Drives the real cmux event bus, not a mock. Owner: lane/qa.

The harness owns two surfaces:

- **cmux socket** — connects to the host-local cmux event bus via the
  events.stream API. Tail-positioned at the most recent heartbeat, retries
  on disconnect, advances on reconnect. Surface IDs and binding tuples
  passed to the test are resolved at event time, never cached.

- **plugin under test** — the cmux plugin process is opened in a private
  workspace with a stub binding pointed at a stable fixture identifier
  (the test's own session, never a real seat). The surface handles
  exposed to the test are explicit plugin-side handles, not direct cmux
  session handles.

Adversarial hooks pivot on a small set of fixture-knobs — each is a
boolean or a deterministic injection point. The harness records
**observations** (literal values) in a capture dict, and the test
emits verdict lines into the standard output. Tests fail on observation
mismatch; verdicts are summary, not the failure mode.

Conventions match CONTRIBUTING.md:
- observations are literal (status codes, event names, counts);
- verdicts live OUTSIDE the data grid (in test docstrings / assertions);
- reopen conditions live in test docstrings, not as escape hatches.

This module is the **surface contract** between Y-CMUX-002 (#1) and
the minimal injector at #3. Any change here breaks both; review
through Aoi before editing.
"""

from __future__ import annotations

import contextlib
import dataclasses
import pathlib
import subprocess
import typing as t
from collections.abc import Iterator

# -- knobs (the seven fixture levers from issue #5) ---------------------


@dataclasses.dataclass(frozen=True)
class _KnobState:
    suppress_composer_submit: bool = False
    focused_surface_id: str | None = None
    two_seats_one_workspace_active: bool = False


# -- observations (the literal-observation capture dict per test) ------


@dataclasses.dataclass
class ObservationCapture:
    """Per-test capture; tests assert against this dataclass's fields.

    The fields are LITERAL OBSERVATIONS:
    - HTTP-style status codes from the plugin (200, 403, etc.)
    - cmux event names ("surface.input_sent", "workspace.prompt.submitted")
    - sequence numbers (int) and event counts (int)
    - timestamps (float seconds since test start, deterministic-enough)
    - sets of delivery_ids / event_ids (literal IDs)

    Verdict lines (PASS / FAIL / HOLD) are NOT stored here — they live
    outside the data grid, in test docstrings / assertions. See CONTRIBUTING.md.
    """

    # cmux bus events observed during the test
    events: list[dict] = dataclasses.field(default_factory=list)

    # plugin → core receipts captured during the test
    plugin_receipts: list[dict] = dataclasses.field(default_factory=list)

    # core → plugin signals received by the test
    core_signals: list[dict] = dataclasses.field(default_factory=list)

    # mid-test fault flags (used by adversarial pivots)
    suppressed_legs: list[str] = dataclasses.field(default_factory=list)

    # final per-deliverable classifications (canonical post-restart set
    # per the yatagarasu cycle closure; see CONTRIBUTING.md /
    # 02-cmux-plugin-acceptance-hooks for terms)
    delivery_classifications: dict[str, str] = dataclasses.field(default_factory=dict)

    # BusObserver three-outcome per delivery_id (Y-CMUX-002 corrected):
    # {NOT_SUBMITTED, UNKNOWN, SUBMITTED}. The harness drives
    # `BusObserver.observe(marker, timeout_s)` for each inject and
    # records the literal return here. Tests assert against this set;
    # the verdict (queued_revert vs held_on_unknown) is driven by
    # this outcome, not by the plugin's `transport-submitted` boolean.
    inject_outcomes: dict[str, str] = dataclasses.field(default_factory=dict)

    def reset(self) -> None:
        """Restore every field to its declared default.

        ``dataclasses.fields(cls).default`` is ``MISSING`` for fields
        declared with ``default_factory=...``, so a naive
        ``setattr(self, field.name, field.default)`` would assign the
        MISSING sentinel to lists/dicts. The correct shape is:
        prefer ``field.default_factory()`` when the factory is set,
        fall back to ``field.default`` for fields with a literal default.
        """
        for field in dataclasses.fields(self):
            if field.default_factory is not dataclasses.MISSING:
                value = field.default_factory()
            else:
                value = field.default
            setattr(self, field.name, value)


# -- the harness context manager -----------------------------------------


@dataclasses.dataclass
class Harness:
    """Owner of the test-side surface. Open via `cmux_harness(...)`.

    Every method returns a literal observation; no method returns a
    classification like "OK" / "PASS". Tests assert against capture
    fields; verdict lines live in the test docstring.
    """

    cmux_socket_path: pathlib.Path
    plugin_binary: pathlib.Path
    plugin_workspace: str
    knob: _KnobState = dataclasses.field(default_factory=_KnobState)
    capture: ObservationCapture = dataclasses.field(default_factory=ObservationCapture)
    _proc: subprocess.Popen | None = None
    _events_tail: t.IO[bytes] | None = None

    # -- the seven fixture levers from issue #5 -------------------------

    def inject(self, identity: str, envelope: dict) -> None:
        """Resolve target by identity (re-resolved on every send).

        Not a cached handle — the resolution happens NOW against the
        cmux surface registry, then the inject call goes out. If the
        identity's surface has moved (e.g., post-restart), the new
        surface is what we hit.

        Envelope is the rendered `[FROM/TO/TYPE/CID]` block plus the
        body; the plugin must see a real attributable turn.

        The plugin mints a marker `[ygr:delivery_id:nonce:sig]`
        (signed HMAC over the four-key contract) per attempt, embeds
        it in the injected text, and the host submit event carries
        the marker for exact correlation. session_id alone is
        session_id alone is insufficient; the marker is what makes
        `BusObserver.observe(marker, timeout_s)` authoritative.
        """
        # Issue #22 builds the production event-stream resident. Until
        # then, raise NotImplementedError with the issue reference so
        # an early caller is told exactly what is missing, rather than
        # silently getting nothing — which is the vacuous-test failure
        # mode (something that cannot fail, therefore cannot inform).
        raise NotImplementedError(
            "harness.inject() awaits the production event-stream resident "
            "tracked in issue #22. The harness is the surface contract; "
            "the resident is the body. Until #22 lands, this is the "
            "honest signal: not the silent-no-op behavior of an ellipsis, "
            "but a fail-loud reference to the issue that owns the gap."
        )

    def suppress_composer_submit(self, on: bool = True) -> None:
        """Adversarial pivot: drop the composer-submit leg.

        When on, the next inject emits `surface.input_sent` only
        (no `workspace.prompt.submitted`). The plugin's correct
        response is one of two outcomes (per Y-CMUX-002 build-lane
        correction 2026-07-18):

        - `BusObserver` returns `NOT_SUBMITTED` (no host events at
          all observed) → the delivery reverts to `queued`; requeue
          is safe because no busy-queue admission is pending.
        - `BusObserver` returns `UNKNOWN` (`surface.input_sent`
          observed, `workspace.prompt.submitted` absent at timeout)
          → the delivery HOLDS in `held_on_unknown`; **no requeue**.
          Reverting here races the cmux busy-queue admission and
          creates a duplicate turn — the exact failure Y-CMUX-017
          exists to prevent.

        Both outcomes carry `transport-submitted = false`. The
        WRONG response is to assume-submitted on the `surface.input_sent`
        alone, OR to revert-on-UNKNOWN. The plugin must distinguish.
        """
        # Use dataclasses.replace — it produces a new instance instead
        # of mutating the frozen _KnobState via object.__setattr__.
        # Bypassing frozen bypasses the immutability contract.
        self.knob = dataclasses.replace(self.knob, suppress_composer_submit=on)
        ...

    def restart_cmux(self, *, boot_id: str | None = None) -> None:
        """Kill cmux (or rotate boot_id if boot_id given) and reconnect.

        Captures the bus event-stream gap. Pairs with the cursor /
        restart tests.
        """
        raise NotImplementedError(
            "harness.restart_cmux() awaits issue #22 (production event-stream "
            "resident). The hook contract for cursor / restart tests is "
            "settled; the harness body that drives it is not. Issue #22 "
            "owns the gap."
        )

    def restart_seat_session(self, identity: str) -> None:
        """Restart the seat's harness session while the plugin holds
        a pending delivery.

        Reproduces the `surface:33 → surface:3` incident. Asserts
        the next inject re-resolves by identity and lands in the
        live surface, not in the cached stale ID.
        """
        raise NotImplementedError(
            "harness.restart_seat_session() awaits issue #22. The Y-CMUX-003 "
            "hook is settled; the seam-injection code that simulates the "
            "restart is the missing piece."
        )

    def force_slow_consumer(self, *, seq: int) -> None:
        """Pause the bus reader until ≥1024 events have passed; force
        the `slow_consumer` disconnect. Asserts reconnect from the
        persisted `seq`.
        """
        raise NotImplementedError(
            "harness.force_slow_consumer() awaits issue #22. Y-CMUX-006 "
            "is the hook; the slow_consumer fixture that drives it is "
            "the missing piece."
        )

    def set_focused_surface(self, surface_id: str) -> None:
        """Drive `surface.focused` to the named surface; used to
        set up banner-withdraw and visibility scenarios.
        """
        raise NotImplementedError(
            "harness.set_focused_surface() awaits issue #23. Y-CMUX-007 "
            "is the hook; the focus-driver that drives it is the missing piece."
        )

    def two_seats_one_workspace(self) -> tuple[str, str]:
        """Provision two seats A (focused) and B (visible but not
        focused) sharing a workspace. Returns (identity_a, identity_b).
        """
        raise NotImplementedError(
            "harness.two_seats_one_workspace() awaits issue #22. The "
            "two-seats-one-workspace fixture is the basis for the "
            "banner-withdraw hook (Y-CMUX-007) and is the missing piece."
        )

    # -- bus reader ----------------------------------------------------

    def _drain_events(self) -> None:
        """Tail the events.jsonl mirror. Records each event into
        self.capture.events as a dict (literal fields, no
        classification).

        Honors the `suppress_composer_submit` knob — when on, we do
        NOT emit the paired `workspace.prompt.submitted` for the
        test's own injects (the plugin must NOT see it).
        """
        ...

    def close(self) -> None:
        if self._events_tail is not None:
            self._events_tail.close()
        if self._proc is not None:
            self._proc.terminate()
            self._proc.wait(timeout=10)


@contextlib.contextmanager
def cmux_harness(
    *,
    cmux_socket_path: pathlib.Path,
    plugin_binary: pathlib.Path,
    plugin_workspace: str,
) -> Iterator[Harness]:
    """Open the harness for a test. Closes on exit.

    The harness connects to the cmux socket, opens the plugin in a
    private workspace, and starts the events-tail reader. Yield
    the Harness; the test calls the fixture levers on it.

    The test owns the observation capture (harness.capture) and the
    verdict lines (test docstring). The harness owns the cmux-side
    lifecycle.
    """
    harness = Harness(
        cmux_socket_path=cmux_socket_path,
        plugin_binary=plugin_binary,
        plugin_workspace=plugin_workspace,
    )
    try:
        # connect cmux socket
        # open plugin process
        # start events tail reader (background thread)
        yield harness
    finally:
        harness.close()


# -- helpers used by tests ------------------------------------------------


def assert_observation_present(
    capture: ObservationCapture,
    *,
    event_name: str | None = None,
    delivery_id: str | None = None,
    event_id: str | None = None,
    receipt_id: str | None = None,
) -> None:
    """Test helper: assert a literal observation is in the capture.

    Used by adversarial tests; the verdict (PASS/FAIL) is the
    PYTEST ASSERTION outcome, not a classification stored in capture.
    """
    if event_name is not None:
        assert any(e.get("name") == event_name for e in capture.events), (
            f"event_name={event_name!r} not in capture.events; "
            f"capture has: {[e.get('name') for e in capture.events]}"
        )
    if delivery_id is not None:
        assert delivery_id in capture.delivery_classifications, (
            f"delivery_id={delivery_id!r} not classified; capture has: "
            f"{list(capture.delivery_classifications)}"
        )
    if event_id is not None:
        assert any(e.get("event_id") == event_id for e in capture.events), (
            f"event_id={event_id!r} not in capture.events"
        )
    if receipt_id is not None:
        assert any(
            r.get("receipt_id") == receipt_id for r in capture.plugin_receipts
        ), f"receipt_id={receipt_id!r} not in capture.plugin_receipts"
