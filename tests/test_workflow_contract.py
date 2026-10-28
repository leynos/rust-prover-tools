"""Contract tests for the mutation-testing caller workflow.

The executable logic lives in the ``leynos/shared-actions`` reusable
workflow, which carries its own unit and integration tests; this repo's
caller is declarative configuration. These tests parse the caller with
PyYAML and assert the contract it must uphold, so drift (repointing the
pin at a branch, widening permissions, selecting the wrong interpreter,
or losing the mutmut configuration) fails CI on the pull request rather
than surfacing in a scheduled or manual run.

The caller must reference the correct reusable workflow at a commit SHA;
Dependabot owns the SHA value, so these tests assert the shape of the
pin (a 40-hex commit SHA on the correct path) rather than a specific
commit.

The caller also selects the interpreter the shared workflow runs the
project on. The shared workflow defaults an omitted ``python-version`` to
``3.13`` and setup-uv exports that selection as ``UV_PYTHON`` for the
whole job, so a caller that omits the input asks ``uv`` to sync this
project on an interpreter its ``requires-python`` excludes; ``uv`` then
refuses the interpreter before collection or mutation. The contract
therefore requires an explicit, string-valued selection inside
``requires-python`` and refuses the shared default.
"""

from __future__ import annotations

import re
import typing as typ
from pathlib import Path

import pytest
import yaml

from tests.conftest import IS_MUTMUT_SANDBOX
from tests.workflow_contracts.coverage_python_sources import requires_python
from tests.workflow_contracts.mutation_caller_sources import (
    SHARED_DEFAULT_PYTHON,
    is_explicit_and_accepted,
    python_version_input,
    resolved_python,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "mutation-testing.yml"
PYPROJECT_PATH = REPO_ROOT / "pyproject.toml"

#: mutmut copies the sources into a ``mutants/`` sandbox that omits ``.github/``,
#: so these tests cannot read the caller there and would fail during the
#: baseline mutmut runs before any mutant, aborting the job. They skip only in
#: that sandbox; in an ordinary run a missing file is a real regression.
pytestmark = pytest.mark.skipif(
    IS_MUTMUT_SANDBOX,
    reason=(
        "mutmut's mutants/ sandbox does not copy .github/, so the caller "
        "cannot be read there"
    ),
)

#: The caller must reference this reusable workflow path, pinned to a
#: 40-hex commit SHA. Dependabot owns the SHA value.
USES_RE = re.compile(
    r"^leynos/shared-actions/\.github/workflows/mutation-mutmut\.yml@[0-9a-f]{40}$"
)

EXPECTED_WITH = {
    "paths": "rust_prover_tools/",
    "module-prefix-strip": "",
    "python-version": "3.14",
}

EXPECTED_CRON = "20 7 * * *"

#: A minimal project declaration for the synthetic callers below, written
#: literally so those cases keep their meaning if this repository's floor
#: moves.
REQUIRES_PYTHON_314: typ.Final[str] = '[project]\nrequires-python = ">=3.14"\n'


def _load() -> dict[str, typ.Any]:
    """Parse the workflow file."""
    return yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))


def _triggers(workflow: dict[str, typ.Any]) -> dict[str, typ.Any]:
    """Return the ``on:`` mapping (PyYAML parses the bare key as True)."""
    triggers = workflow.get("on", workflow.get(True))
    assert isinstance(triggers, dict), "the workflow must declare an on: mapping"
    return triggers


def _mutation_job(workflow: dict[str, typ.Any]) -> dict[str, typ.Any]:
    """Return the single calling job."""
    jobs = workflow.get("jobs")
    assert isinstance(jobs, dict), "the workflow must declare a jobs mapping"
    assert list(jobs) == ["mutation"], (
        f"expected a single job named 'mutation', found {sorted(jobs)}"
    )
    return jobs["mutation"]


def _caller(declaration: str) -> str:
    """Render a mutation caller whose ``with:`` block holds ``declaration``.

    The declaration is inserted as YAML text so each case can spell the
    input exactly as a caller might, including the quoting that decides
    whether YAML hands the workflow a string or a number.
    """
    return (
        "jobs:\n"
        "  mutation:\n"
        "    uses: leynos/shared-actions/.github/workflows/"
        f"mutation-mutmut.yml@{'0' * 40}\n"
        "    with:\n"
        f"      {declaration}"
    )


def test_uses_reference_is_pinned_to_a_full_commit_sha() -> None:
    """The job must call the shared workflow, pinned to a 40-hex commit SHA."""
    uses = _mutation_job(_load()).get("uses")
    assert uses is not None, "jobs.mutation.uses is missing"
    assert USES_RE.match(uses), (
        f"jobs.mutation.uses must reference mutation-mutmut.yml pinned to a "
        f"full 40-character lowercase hex commit SHA, not a branch or tag: "
        f"{uses!r}"
    )


def test_job_permissions_are_exactly_least_privilege() -> None:
    """The job grants contents: read and id-token: write, nothing broader."""
    permissions = _mutation_job(_load()).get("permissions")
    assert permissions == {"contents": "read", "id-token": "write"}, (
        "jobs.mutation.permissions must be exactly "
        f"{{'contents': 'read', 'id-token': 'write'}}, got {permissions!r}"
    )


def test_workflow_default_permissions_are_empty() -> None:
    """The workflow-level default token scope is empty."""
    workflow = _load()
    assert workflow.get("permissions") == {}, (
        f"top-level permissions must be an empty mapping, got "
        f"{workflow.get('permissions')!r}"
    )


def test_concurrency_serializes_per_ref_without_cancelling() -> None:
    """Runs queue per ref instead of cancelling one another."""
    concurrency = _load().get("concurrency")
    assert isinstance(concurrency, dict), "the workflow must declare concurrency"
    assert concurrency.get("group") == "mutation-testing-${{ github.ref }}", (
        f"concurrency.group must key on the triggering ref, got "
        f"{concurrency.get('group')!r}"
    )
    assert concurrency.get("cancel-in-progress") is False, (
        f"concurrency.cancel-in-progress must be false, got "
        f"{concurrency.get('cancel-in-progress')!r}"
    )


def test_triggers_keep_schedule_and_plain_dispatch() -> None:
    """The daily schedule stays; dispatch declares no inputs."""
    triggers = _triggers(_load())
    schedule = triggers.get("schedule")
    assert schedule == [{"cron": EXPECTED_CRON}], (
        f"on.schedule must be the daily 07:20 UTC cron, got {schedule!r}"
    )
    assert "workflow_dispatch" in triggers, "on.workflow_dispatch is missing"
    dispatch = triggers.get("workflow_dispatch") or {}
    inputs = (dispatch or {}).get("inputs") or {}
    assert inputs == {}, f"on.workflow_dispatch must not declare inputs, got {inputs!r}"


def test_with_block_carries_the_caller_configuration() -> None:
    """The caller passes the flat-layout paths, prefix-strip, and Python."""
    with_block = _mutation_job(_load()).get("with")
    assert with_block == EXPECTED_WITH, (
        f"jobs.mutation.with must equal {EXPECTED_WITH!r}, got {with_block!r}"
    )


def test_the_caller_selects_a_python_the_project_accepts() -> None:
    """The caller declares a string Python explicitly and inside requires-python."""
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
    pyproject = PYPROJECT_PATH.read_text(encoding="utf-8")
    selection = python_version_input(workflow)
    assert selection is not None, (
        "jobs.mutation.with must declare python-version explicitly; an "
        f"omitted input falls back to the shared {SHARED_DEFAULT_PYTHON} "
        "default"
    )
    assert isinstance(selection, str), (
        "jobs.mutation.with must declare python-version as a quoted string; "
        f"an unquoted 3.14 is a YAML float and 3.10 reads back as 3.1, got "
        f"{selection!r}"
    )
    assert selection, (
        "jobs.mutation.with must declare a non-empty python-version; an "
        "explicit empty string does not take the shared default and is "
        "rejected by the shared workflow's version check"
    )
    assert is_explicit_and_accepted(workflow, pyproject), (
        f"the caller selects Python {selection}, outside the project's "
        f"requires-python {requires_python(pyproject)}; raise the caller's "
        "selection in the same change as the floor"
    )


#: One in-memory caller declaring ``python-version`` as spelled in
#: ``declaration``. ``raw`` is what PyYAML reads back, which separates a
#: quoted string from a float and an omitted input (``None``) from an
#: explicit empty string; ``effective`` is what the shared workflow runs.
class SyntheticCaller(typ.NamedTuple):
    """One caller's ``python-version`` spelling and the two ways to read it."""

    declaration: str
    raw: object
    effective: str


#: The cases the interpreter contract is judged on. ``UNQUOTED_FLOAT`` is the
#: ``3.14`` a caller writes without quotes, which YAML hands over as a number
#: rather than the string the shared workflow expects. It is compared, never
#: used, and Ruff's ``math-constant`` suggestion would change the value.
UNQUOTED_FLOAT: typ.Final[float] = 3.14  # ruff: ignore[math-constant]
CALLERS: typ.Final[tuple[SyntheticCaller, ...]] = (
    # Omission is the only spelling that gets the shared workflow's default.
    SyntheticCaller("", None, SHARED_DEFAULT_PYTHON),
    # An explicit empty value is passed through; GitHub Actions applies an
    # input default only when the caller omits the input (actions/runner#2907).
    SyntheticCaller('python-version: ""\n', "", ""),
    SyntheticCaller('python-version: "3.13"\n', "3.13", "3.13"),
    SyntheticCaller("python-version: 3.14\n", UNQUOTED_FLOAT, "3.14"),
    SyntheticCaller('python-version: "3.14"\n', "3.14", "3.14"),
    SyntheticCaller('python-version: "3.15"\n', "3.15", "3.15"),
)
CALLER_IDS: typ.Final[list[str]] = [
    "omitted",
    "empty",
    "older-string",
    "unquoted-float",
    "baseline",
    "newer",
]
#: The callers the contract accepts: explicit, string-valued, and inside the
#: literal ``>=3.14`` range. The rest must be rejected, because dropping the
#: input silently restores the shared default outside that range.
ACCEPTED_CALLERS: typ.Final[frozenset[str]] = frozenset({"baseline", "newer"})


@pytest.mark.parametrize("caller", CALLERS, ids=CALLER_IDS)
def test_the_selection_resolves_to_the_shared_default_only_when_absent(
    caller: SyntheticCaller,
) -> None:
    """Only an omitted input resolves to the shared 3.13 default."""
    selection = python_version_input(_caller(caller.declaration))

    assert selection == caller.raw, f"declared {selection!r}, expected {caller.raw!r}"
    assert resolved_python(selection) == caller.effective, (
        f"declared {selection!r} resolves to {resolved_python(selection)!r}, "
        f"expected {caller.effective!r}"
    )


@pytest.mark.parametrize(
    ("caller", "identifier"),
    tuple(zip(CALLERS, CALLER_IDS, strict=True)),
    ids=CALLER_IDS,
)
def test_only_an_explicit_compatible_string_is_accepted(
    caller: SyntheticCaller, identifier: str
) -> None:
    """Omission, emptiness, an older version and a YAML float all fail."""
    # The same judgement runs over the checked-in caller, so these cases cannot
    # drift from the contract the repository itself is held to.
    assert is_explicit_and_accepted(
        _caller(caller.declaration), REQUIRES_PYTHON_314
    ) is (identifier in ACCEPTED_CALLERS), (
        f"caller {identifier} declared {caller.raw!r}"
    )


@pytest.mark.parametrize(
    "workflow",
    [
        "",
        "- a list\n",
        "jobs: scalar\n",
        "jobs:\n  mutation: scalar\n",
        "jobs:\n  mutation: [x]\n",
        "jobs:\n  mutation:\n    with: scalar\n",
    ],
    ids=[
        "empty",
        "list-top-level",
        "scalar-jobs",
        "scalar-job",
        "list-job",
        "scalar-with",
    ],
)
def test_a_malformed_caller_reads_as_no_selection(workflow: str) -> None:
    """A wrongly shaped workflow, job or ``with`` mapping declares nothing."""
    selection = python_version_input(workflow)

    assert selection is None, (
        f"{workflow!r} should read as an omitted input, got {selection!r}"
    )
