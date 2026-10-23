"""Guard the mutmut-only skips for the directory-changing tests.

mutmut aborts the whole run when any test fails, so the tests that change the
working directory and then call mutated code must be skipped inside its
sandbox. That skip is easy to break silently: the tests pass in an ordinary
run either way, and the failure only appears in a scheduled mutation run.

These tests check the skip predicate itself rather than pytest's handling of
the marker, so they hold under mutmut as well as in an ordinary run.
"""

from __future__ import annotations

import typing as typ

from rust_prover_tools.unittests import conftest

if typ.TYPE_CHECKING:
    import pytest


def test_the_guard_is_active_under_mutmut(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The skip predicate reports True while mutmut runs the tests."""
    monkeypatch.setenv(conftest.MUTMUT_UNDER_TEST_ENV, "stats")

    assert conftest.is_mutmut_running(), (
        "a mutmut run must be recognised, or the chdir tests fail the baseline"
    )


def test_the_guard_is_inert_outside_mutmut(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ordinary run reports False, so the tests keep their normal coverage."""
    monkeypatch.delenv(conftest.MUTMUT_UNDER_TEST_ENV, raising=False)

    assert not conftest.is_mutmut_running(), (
        "an ordinary run must not skip the chdir tests"
    )
