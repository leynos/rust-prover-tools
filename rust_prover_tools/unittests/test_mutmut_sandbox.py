"""Guard the mutmut-only skips for the directory-changing tests.

mutmut aborts the whole run when any test fails, so the tests that change the
working directory and then call mutated code must be skipped inside its
sandbox. That skip is easy to break silently: the tests pass in an ordinary
run either way, and the failure only appears in a scheduled mutation run.

These tests cover both halves of the guard -- the environment reading that
decides whether mutmut is running, and the markers that apply it to the
affected tests -- so removing either one fails an ordinary pull request.

The marker can only be derived from a process's environment before its
``conftest`` is imported, and the process running these tests already has its
own environment fixed. Each case therefore inspects a fresh interpreter, so
the same assertions hold both with and without mutmut's marker rather than
only in whichever mode this run happens to be in.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import typing as typ
from pathlib import Path

import pytest

from rust_prover_tools.unittests import conftest, test_kani, test_verus_install

if typ.TYPE_CHECKING:
    import types

#: The repository root, which holds the package the probe imports.
REPO_ROOT = Path(__file__).resolve().parents[2]

#: Run in a fresh interpreter after its marker has been set. It reports the
#: expectation that process derives from its own environment, the flag its
#: ``conftest`` cached, and each guarded test's marker condition, so the
#: assertions compare an independently derived expectation with the values the
#: process actually uses. A regression that hardcodes either side fails one of
#: the two marker cases below.
PROBE: typ.Final[str] = """
import json
import os
from rust_prover_tools.unittests import conftest, test_mutmut_sandbox as sandbox

print(json.dumps({
    "expected": conftest.reads_mutmut_marker(os.environ),
    "is_mutmut_run": conftest.IS_MUTMUT_RUN,
    "conditions": {
        f"{module.__name__}.{name}": sandbox.skip_condition(getattr(module, name))
        for module, name in sandbox.GUARDED_TESTS
    },
}))
"""


#: The tests that change the working directory and then call mutated code,
#: each paired with the module it lives in. They are exactly the tests mutmut
#: cannot run; losing either marker restores the aborted baseline.
GUARDED_TESTS: typ.Final[tuple[tuple[types.ModuleType, str], ...]] = (
    (
        test_kani,
        "test_check_kani_version_resolves_relative_version_file_from_repo_root",
    ),
    (
        test_verus_install,
        "test_install_verus_resolves_relative_pin_files_from_repo_root",
    ),
)


def skip_condition(test: object) -> object:
    """Return the condition of ``test``'s ``skipif`` marker, or ``None``."""
    conditions = [
        marker.args[0]
        for marker in _markers(test)
        if marker.name == "skipif" and marker.args
    ]
    return conditions[0] if conditions else None


def _markers(test: object) -> list[typ.Any]:
    """Return the pytest markers attached to ``test``."""
    return list(getattr(test, "pytestmark", []))


def _probe(marker: str | None) -> dict[str, object]:
    """Run ``PROBE`` in a fresh interpreter, with or without mutmut's marker."""
    environment = dict(os.environ)
    if marker is None:
        environment.pop(conftest.MUTMUT_UNDER_TEST_ENV, None)
    else:
        environment[conftest.MUTMUT_UNDER_TEST_ENV] = marker

    completed = subprocess.run(
        [sys.executable, "-c", PROBE],
        cwd=REPO_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(completed.stdout)


@pytest.mark.parametrize(
    ("marker", "is_marked"),
    [
        (None, False),
        ("stats", True),
        ("", True),
    ],
    ids=["ordinary-run", "stats", "empty-name"],
)
def test_the_guard_follows_the_marker_in_a_fresh_process(
    marker: str | None, *, is_marked: bool
) -> None:
    """A process's guard and markers both follow its own environment."""
    probe = _probe(marker)

    assert probe["expected"] is is_marked, (
        f"a process with marker {marker!r} should derive {is_marked}"
    )
    assert probe["is_mutmut_run"] == is_marked, (
        f"IS_MUTMUT_RUN read as {probe['is_mutmut_run']!r} in a process whose "
        f"marker is {marker!r}, expected {is_marked}"
    )
    mismatched = {
        name: condition
        for name, condition in typ.cast(
            "dict[str, object]", probe["conditions"]
        ).items()
        if condition != is_marked
    }
    assert not mismatched, (
        f"these tests do not follow a marker of {marker!r} ({is_marked}): {mismatched}"
    )


def test_each_chdir_test_carries_the_guard_marker() -> None:
    """Removing a marker from an affected test fails this ordinary run.

    Only the marker's presence and condition are checked, so this says nothing
    about whether pytest applied it in the current process.
    """
    unguarded = [
        name
        for module, name in GUARDED_TESTS
        if skip_condition(getattr(module, name)) != conftest.IS_MUTMUT_RUN
    ]

    assert not unguarded, (
        "these tests lose their mutmut skip, so a mutation run would abort "
        f"during its baseline: {unguarded}"
    )
