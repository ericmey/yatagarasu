"""Import smoke tests.

Trivial by design, but not pointless: they fail loudly if a package root is
misdeclared or an __init__ breaks, which otherwise surfaces as a confusing
collection error inside an unrelated lane's test run.

They also keep CI honest before feature tests exist — a suite that collects
nothing exits non-zero, and "no tests ran" must never be mistaken for "tests
passed".
"""

from __future__ import annotations

import importlib

import pytest

PACKAGES = [
    "yatagarasu_core",
    "yatagarasu_cmux",
    "yatagarasu_agent_bridge",
    "yatagarasu_discord",
]


@pytest.mark.parametrize("name", PACKAGES)
def test_package_imports(name: str) -> None:
    module = importlib.import_module(name)
    assert module.__name__ == name
