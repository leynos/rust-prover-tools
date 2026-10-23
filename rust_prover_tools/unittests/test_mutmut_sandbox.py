"""Guard the mutmut-only skips for the directory-changing tests.

mutmut aborts the whole run when any test fails, so the tests that change the
working directory and then call mutated code must be skipped inside its
sandbox. That skip is easy to break silently: the tests pass in an ordinary
run either way, and the failure only appears in a scheduled mutation run.

These tests cover both halves of the guard -- the environment reading that
decides whether mutmut is running, and the markers that apply it to the
affected tests -- so removing either one fails an ordinary pull request.
"""

from __future__ import annotations

import typing as typ

import pytest

from rust_prover_tools.unittests import conftest, test_kani, test_verus

if typ.TYPE_CHECKING:
    import types


#: The tests that change the working directory and then call mutated code,
#: each paired with the module it lives in. They are exactly the tests mutmut
#: cannot run; losing either marker restores the aborted baseline.
GUARDED_TESTS: typ.Final[tuple[tuple[types.ModuleType, str], ...]] = (
    (
        test_kani,
        "test_check_kani_version_resolves_relative_version_file_from_repo_root",
    ),
    (
        test_verus,
        "test_install_verus_resolves_relative_pin_files_from_repo_root",
    ),
)


@pytest.mark.parametrize(
    ("environment", "is_marked"),
    [
        ({}, False),
        ({"UNRELATED": "1"}, False),
        ({conftest.MUTMUT_UNDER_TEST_ENV: "stats"}, True),
        ({conftest.MUTMUT_UNDER_TEST_ENV: ""}, True),
    ],
    ids=["empty", "unrelated-variable", "stats", "empty-name"],
)
def test_the_guard_reads_the_mutmut_marker(
    environment: dict[str, str],
    *,
    is_marked: bool,
) -> None:
    """Presence of the marker marks a mutation run; its value does not matter."""
    assert conftest.reads_mutmut_marker(environment) is is_marked, (
        f"environment {environment!r} should read as {is_marked}"
    )


def _skip_conditions(test: object) -> list[object]:
    """Return the conditions of every ``skipif`` marker on ``test``."""
    markers = getattr(test, "pytestmark", [])
    return [
        marker.args[0] for marker in markers if marker.name == "skipif" and marker.args
    ]


def test_each_chdir_test_carries_the_guard_marker() -> None:
    """Removing a marker from an affected test fails this ordinary run.

    Only the marker's presence and condition are checked, so this says nothing
    about whether pytest applied it in the current process.
    """
    unguarded = [
        name
        for module, name in GUARDED_TESTS
        if conftest.IS_MUTMUT_RUN not in _skip_conditions(getattr(module, name))
    ]

    assert not unguarded, (
        "these tests lose their mutmut skip, so a mutation run would abort "
        f"during its baseline: {unguarded}"
    )
