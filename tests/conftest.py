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
# Default floor tracks the count of HONESTLY skipped hooks with
# tracked issues. As of 2026-07-19 the suite has 4 such skips, each
# referencing an open issue in the path-to-completion plan (#37):
#   Y-CMUX-007 -> #23  Y-CMUX-010 -> #26
#   Y-CMUX-012 -> #28  Y-CMUX-014 -> #29
# (Y-CMUX-008 -> #24 landed in PR #39, floor 7 -> 6. Y-CMUX-009 -> #25
# landed in PR #40, floor 6 -> 5. Y-CMUX-006 -> #22 began passing in
# PR #44 but the floor was NOT lowered with it; caught 2026-07-19 and
# dropped 5 -> 4. A ratchet that is only ever tightened by hand is not
# a ratchet — a regression from 4 skips back to 5 would have passed
# silently for as long as the floor stayed stale.)
# Raising the floor requires either:
#   1. A new tracked skip (add a line above with its issue ref), OR
#   2. Resolving an existing issue and converting the skip to a pass
#      (lower the floor by one and remove the line).
# An UNTRACKED skip is exactly the failure mode this conftest is
# built to detect; see the "a skip beats a lie" rule in issue #37.
_DEFAULT_FLOOR = 4


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
            "Maximum number of skip OUTCOMES permitted in a test run, "
            "counted from the terminal reporter's `stats['skipped']` "
            "list (one entry per skipped test or subtest). Default "
            "tracks the count of honestly-tracked skips (see "
            "_DEFAULT_FLOOR above); raise only with documented "
            "justification. The implementation counts skip outcomes, "
            "not pytest.skip() invocations directly — if a single "
            "test calls skip() twice, only one outcome is recorded "
            "(pytest's terminal reporter dedupes); the count tracks "
            "the number of tests/items that ended in skip status."
        ),
    )


def pytest_report_header(config: pytest.Config) -> str:
    """Surface the floor in the report header so a human sees it."""
    return f"yatagarasu: skip-floor = {config.getoption('--skip-floor')}"


def pytest_sessionfinish(
    session: pytest.Session,
    exitstatus: pytest.ExitCode,
) -> None:
    """Count skips and fail the session if they exceed the floor.

    Hooking into pytest_sessionfinish (which runs before
    pytest_terminal_summary) is what converts the floor from a
    cosmetic signal into a CI-enforced constraint. Without this hook,
    the green checkmark would still pass even when a skip exceeded
    the floor.
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
    if skipped > floor:
        # Setting exitstatus here is what fails the CI check job.
        # The terminal summary hook below prints the human-readable
        # message that explains why.
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
    if skipped > floor:
        terminalreporter.write_line(
            f"FAIL: {skipped} skips exceed the floor of {floor}. "
            f"Either remove the skips or raise the floor with documented "
            f"justification. This is the suite-scope partial-evidence "
            f"rule from CONTRIBUTING.md.",
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
