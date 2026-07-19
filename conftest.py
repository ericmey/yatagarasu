"""Pytest config: enforce a skip floor on the acceptance suite.

This is the suite-scope version of the rule that a partially-run
acceptance suite is no evidence at all. Skips at suite level are the
acceptance-suite equivalent of acceptance hooks being unbuilt; each
one is an item the test session reports as green but actually did not
exercise. A skip that lands without a conscious decision is a quiet
false-pass.

The floor default lives in ``_DEFAULT_FLOOR`` below. Raising or
lowering the default is a deliberate act with rationale documented
inline; the floor is overridden by the env var
``YATAGARASU_SKIP_FLOOR`` for the rare case where a skip is justified
during a transitional state (e.g. a fixture is in flight).

The skip count is also surfaced in the terminal summary so a human
reading the CI output sees the count, not just the green checkmark.

See CONTRIBUTING.md for the broader partial-evidence rule this
config implements.
"""

from __future__ import annotations

import os
import sys

import pytest

_SKIP_FLOOR_ENV = "YATAGARASU_SKIP_FLOOR"
# Default floor is the EXACT count of honestly-skipped hooks with tracked
# issues — not a ceiling. As of 2026-07-19 there are 2, each referencing an
# open issue in the path-to-completion plan (#37):
#   Y-CMUX-010 -> #26  Y-CMUX-014 -> #29
#
# History, kept because it is the argument for the `!=` check below:
#   Y-CMUX-008 -> #24 landed in PR #39, floor 7 -> 6.
#   Y-CMUX-009 -> #25 landed in PR #40, floor 6 -> 5.
#   Y-CMUX-006 -> #22 began passing in PR #44; floor NOT lowered. Caught by
#     hand 2026-07-19, dropped 5 -> 4.
#   Y-CMUX-007 -> #23 began passing in PR #54 hours later; floor NOT lowered
#     again. That second miss is why this stopped being a convention: the
#     enforcement now fails on ANY divergence, so closing a skip without
#     moving the floor is caught in the run that closes it.
#   Y-CMUX-012 -> #28 began passing in PR #42 once the injector minted through
#     MarkerAuthority and the emitter could finally see a marker. Floor 3 -> 2,
#     and the mechanism did the reminding: the run that closed the skip failed.
#     Note the `!=` gate guards the NUMBER; nothing guards this prose beside it,
#     and it went stale here once already. Change both together.
#
# Changing the floor requires either:
#   1. A new tracked skip (add its issue ref to the list above), OR
#   2. Closing a skip (delete its line above and lower the number below).
# Both directions are enforced. An UNTRACKED skip is the failure mode this
# conftest was built to detect; a STALE floor is the one it kept missing.
# See the "a skip beats a lie" rule in issue #37.
_DEFAULT_FLOOR = 2


def pytest_addoption(parser: pytest.Parser) -> None:
    """Add a --skip-floor CLI option that overrides the env-var default.

    CI uses the env var; humans running locally can pass
    ``--skip-floor=N`` to bypass during transitional states.

    The env var is parsed defensively: a typo or non-numeric value
    falls back to ``_DEFAULT_FLOOR`` with a warning rather than
    crashing pytest collection with a ValueError. Everyone runs this
    file; a typo in an env var should not make the suite unstartable.
    """

    default_floor = _DEFAULT_FLOOR
    raw_env = os.environ.get(_SKIP_FLOOR_ENV)
    if raw_env is not None:
        try:
            default_floor = int(raw_env)
        except ValueError:
            # Use sys.stderr.write rather than warnings.warn because the
            # project policy is filterwarnings=["error"] — any UserWarning
            # becomes a test-collection failure. This is an operational
            # message to the human who typed the env var, not a code-quality
            # warning; the strict policy doesn't apply.
            sys.stderr.write(
                f"yatagarasu: {_SKIP_FLOOR_ENV}={raw_env!r} is not a valid "
                f"integer; falling back to default floor={_DEFAULT_FLOOR}. "
                f"Fix the env var (set to an integer like '6') to silence.\n"
            )
            default_floor = _DEFAULT_FLOOR

    parser.addoption(
        "--skip-floor",
        action="store",
        type=int,
        default=default_floor,
        help=(
            "EXACT number of skipped tests expected in a run — not a maximum. "
            "The run fails if the count diverges in either direction, so "
            "closing a skip without lowering the floor is caught in the run "
            "that closes it. Default tracks the honestly-tracked skips (see "
            "_DEFAULT_FLOOR above); change it only with documented "
            "justification."
        ),
    )


def pytest_report_header(config: pytest.Config) -> str:
    """Surface the floor in the report header so a human sees it."""
    return f"yatagarasu: skip-floor = {config.getoption('--skip-floor')}"


def pytest_sessionfinish(
    session: pytest.Session,
    exitstatus: pytest.ExitCode,
) -> None:
    """Count skips and fail the session if they DIVERGE from the floor.

    Not "exceed". The floor is an exact expected count, in both directions —
    see the `!=` below and the rationale beside it.

    Hooking into pytest_sessionfinish (which runs before
    pytest_terminal_summary) is what converts the floor from a
    cosmetic signal into a CI-enforced constraint. Without this hook,
    the green checkmark would still pass when the skip count diverged
    from the floor in either direction.
    """
    floor = session.config.getoption("--skip-floor")
    rep = session.config.pluginmanager.getplugin("terminalreporter")
    if rep is None:  # pragma: no cover - defensive, the plugin is always present
        return
    skipped = len(rep.stats.get("skipped", []))
    xfailed = len(rep.stats.get("xfailed", []))
    xpassed = len(rep.stats.get("xpassed", []))
    # Stash the counts on the terminal reporter so pytest_terminal_summary
    # can read them in the same form without re-counting.
    # (A previous revision stashed on `session` instead of `terminalreporter`
    # and pytest_terminal_summary never read them — the dead-cache bug.)
    rep._yatagarasu_skip_counts = (skipped, xfailed, xpassed, floor)
    if skipped != floor and exitstatus in {
        pytest.ExitCode.OK,
        pytest.ExitCode.TESTS_FAILED,
    }:
        # Only override an ordinary pass/fail. Copilot caught the first draft
        # overwriting unconditionally, which would report INTERRUPTED,
        # INTERNAL_ERROR, USAGE_ERROR or NO_TESTS_COLLECTED as a skip-floor
        # failure — a crashed or mis-invoked run wearing the wrong cause of
        # death. A gate that misreports WHY the run failed is a gate you learn
        # to distrust, which is worse than not having it.
        #
        # Setting exitstatus here is what fails the CI check job.
        # The terminal summary hook below prints the human-readable
        # message that explains why.
        #
        # `!=` rather than `>`, changed 2026-07-19 after the floor went stale
        # TWICE IN ONE MORNING. Y-CMUX-006 began passing in PR #44 and the floor
        # stayed at 5; that was caught by hand and dropped to 4. Hours later
        # PR #54 took the count to 3 and the floor stayed at 4 again.
        #
        # Under `>`, closing a skip is silent: the count falls below the floor,
        # nothing complains, and the floor now permits a regression back to the
        # old number. A ratchet that only ever tightens when somebody remembers
        # to tighten it is not a ratchet — it is a comment.
        #
        # Under `!=`, the gate fails the moment the counts diverge in EITHER
        # direction, so the person who closed the skip is the person told to
        # lower the floor, in the run where they closed it. That is the whole
        # difference between a convention and a mechanism.
        session.exitstatus = pytest.ExitCode.TESTS_FAILED


def pytest_terminal_summary(
    terminalreporter: pytest.TerminalReporter,
    exitstatus: pytest.ExitCode,
    config: pytest.Config,
) -> None:
    """Print a single-line skip summary at the end of the session.

    A green checkmark with no skip count visible is exactly the
    partial-evidence failure mode this conftest exists to prevent.
    """
    counts = getattr(terminalreporter, "_yatagarasu_skip_counts", None)
    if counts is None:
        floor = config.getoption("--skip-floor")
        skipped = len(terminalreporter.stats.get("skipped", []))
        xfailed = len(terminalreporter.stats.get("xfailed", []))
        xpassed = len(terminalreporter.stats.get("xpassed", []))
    else:
        skipped, xfailed, xpassed, floor = counts

    line = (
        f"yatagarasu-skip-count: skipped={skipped} "
        f"xfailed={xfailed} xpassed={xpassed} floor={floor}"
    )
    terminalreporter.write_sep("=", "yatagarasu", yellow=True)
    terminalreporter.write_line(line)

    # Same guard as pytest_sessionfinish, for the same reason. I fixed the exit
    # code to stop masking INTERRUPTED / INTERNAL_ERROR / USAGE_ERROR /
    # NO_TESTS_COLLECTED and left this printer shouting FAIL over them anyway —
    # half a fix, which reads to an operator as the whole diagnosis. Copilot
    # caught the half I missed.
    if exitstatus not in {pytest.ExitCode.OK, pytest.ExitCode.TESTS_FAILED}:
        return

    if skipped > floor:
        terminalreporter.write_line(
            f"FAIL: {skipped} skips exceed the floor of {floor}. "
            f"Either remove the skips or raise the floor with documented "
            f"justification. This is the suite-scope partial-evidence "
            f"rule from CONTRIBUTING.md.",
            red=True,
        )
    elif skipped < floor:
        # Name the right remediation for where the floor actually came from.
        # Telling someone to edit _DEFAULT_FLOOR when their floor arrived via
        # --skip-floor or the env var sends them to edit a value the run is not
        # reading — an instruction that cannot work is its own small alibi.
        overridden = (
            config.getoption("--skip-floor") != _DEFAULT_FLOOR
            or os.environ.get(_SKIP_FLOOR_ENV) is not None
        )
        remedy = (
            f"the floor is overridden (--skip-floor / {_SKIP_FLOOR_ENV}); "
            f"adjust or unset that override"
            if overridden
            else f"lower _DEFAULT_FLOOR to {skipped} in conftest.py and "
            f"delete the closed skip's line from the tracked list above it"
        )
        # A partial run trips this too, and that is deliberate rather than
        # incidental: the floor describes the FULL suite, so a subtree run
        # producing fewer skips is not a smaller pass, it is not the gate at
        # all. Saying so out loud is the suite-scope version of Tama's rule
        # that a partially-run suite is no evidence.
        partial = set(config.args or ()) - {str(config.rootpath)}
        if partial:
            remedy = (
                f"this looks like a PARTIAL run ({', '.join(sorted(partial))}). "
                f"The floor describes the whole suite, so this count is not "
                f"evidence about it — run `make check` for the real gate. If "
                f"you did mean to change the floor, {remedy}"
            )
        terminalreporter.write_line(
            f"FAIL: {skipped} skips are BELOW the floor of {floor}. "
            f"You closed a skip — well done — and the floor did not move with "
            f"it. To fix: {remedy}. Until then the floor still permits a "
            f"regression back to {floor} skips, which would pass silently.",
            red=True,
        )


def pytest_configure(config: pytest.Config) -> None:
    """Conftest hook reserved for future marker registration.

    The skip-floor discipline is enforced in pytest_sessionfinish;
    no custom marker is needed. A custom marker was registered in
    an earlier revision but was unused — pytest.mark.skip with a
    documented `reason=` string carries the per-skip rationale that
    the discipline requires, and the floor tracks the count via the
    terminalreporter's stats directly. Leaving this hook in place
    as the natural place to register any future marker should the
    team want one; today it does nothing, by design.
    """
    return None
