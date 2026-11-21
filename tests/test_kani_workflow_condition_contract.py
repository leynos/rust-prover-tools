"""Contract tests for the job-level conditions in the snapshot workflow.

GitHub applies an implicit `success()` check over a job's `needs`, and a
job-level `if` replaces that check only when the condition calls a status-check
function such as `always()` or `!cancelled()`. An ordinary comparison leaves
the implicit check in place, so a failed prerequisite stops the next job even
when its own condition would otherwise pass.

No job in this workflow accepts a skipped prerequisite, so every condition is
an ordinary comparison and the implicit check is what guards the chain. Each
test below pins one consequence of that rule, so a condition edited for
readability cannot silently replace the check and let a job run after a
prerequisite it does not accept.

The guard, permissions, budgets and artefact routing are pinned in
`tests/test_kani_workflow_contract.py`; the build job's hand-offs are pinned in
`tests/test_kani_workflow_build_contract.py`.
"""

from __future__ import annotations

import typing as typ

import pytest

from tests.workflow_contracts.strict_workflow import (
    dependency_names,
    job,
    read_workflow,
    run_scripts,
    suppresses_implicit_success,
)

#: Every job that repeats the trusted-ref guard before doing any work.
GUARDED_JOBS: typ.Final[tuple[str, ...]] = (
    "resolve",
    "build",
    "assemble",
    "validate",
    "publish",
)


@pytest.fixture(scope="module")
def workflow() -> dict[str, object]:
    """Return the parsed workflow shared by the whole module."""
    return read_workflow()


def test_every_trusted_job_repeats_the_guard(
    workflow: dict[str, object],
) -> None:
    """A job that skipped the guard would run from an untrusted ref."""
    for name in GUARDED_JOBS:
        declaration = job(workflow, name)
        assert dependency_names(declaration)[0] == "guard", name
        assert "needs.guard.outputs.trusted == 'true'" in typ.cast(
            "str", declaration["if"]
        ), name

    guard = "\n".join(run_scripts(job(workflow, "guard")))
    assert 'GITHUB_REPOSITORY" != "$CANONICAL_REPOSITORY' in guard
    assert 'GITHUB_REF" != "refs/heads/main' in guard


@pytest.mark.parametrize(
    ("name", "prerequisite"),
    [
        ("build", "resolve"),
        ("assemble", "build"),
        ("validate", "assemble"),
        ("publish", "validate"),
    ],
)
def test_a_failed_prerequisite_stops_the_next_job(
    workflow: dict[str, object],
    name: str,
    prerequisite: str,
) -> None:
    """Each job names the prerequisite result it accepts.

    An ordinary comparison keeps the implicit `success()` check, so naming the
    prerequisite's result states which outcomes the job accepts rather than
    replacing that check. Without it, a job that is deliberately allowed to
    run after a failure could not say which failure it tolerates.
    """
    condition = typ.cast("str", job(workflow, name)["if"])
    assert f"needs.{prerequisite}.result == 'success'" in condition, name
    assert prerequisite in dependency_names(job(workflow, name)), name


@pytest.mark.parametrize(
    "condition",
    [
        "needs.build.result == 'success'",
        "github.event_name == 'workflow_dispatch' && needs.build.result == 'success'",
    ],
    ids=["comparison", "comparison-with-event"],
)
def test_an_ordinary_comparison_keeps_the_implicit_success_check(
    condition: str,
) -> None:
    """A comparison is not a status check, so the default result check stays."""
    assert not suppresses_implicit_success(condition)


@pytest.mark.parametrize(
    ("condition", "suppresses"),
    [
        ("always()", True),
        ("!cancelled() && needs.build.result == 'success'", True),
        ("failure()", True),
        ("success() && needs.build.result == 'success'", True),
        # A result value is not a call, and a comparison that merely mentions
        # a status is not the status-check function the rule names.
        ("contains(needs.build.result, 'fail') == false", False),
        ("needs.x.result == 'failure'", False),
        ("needs.x.outcome == 'cancelled'", False),
    ],
    ids=[
        "always",
        "not-cancelled",
        "failure",
        "success",
        "status-word-without-call",
        "failure-value",
        "cancelled-value",
    ],
)
def test_a_status_check_replaces_the_implicit_success_check(
    condition: str,
    *,
    suppresses: bool,
) -> None:
    """Only a real status-check call replaces the default result check."""
    assert suppresses_implicit_success(condition) is suppresses


def test_no_condition_replaces_the_implicit_success_check(
    workflow: dict[str, object],
) -> None:
    """A status-check function would let a job run after a failed prerequisite.

    Every guarded job depends on a prerequisite whose failure must stop it,
    and none of them accepts a skipped result. A status-check function in any
    of these conditions would therefore replace the implicit `success()`
    check without naming a failure the job tolerates.
    """
    for name in GUARDED_JOBS:
        condition = typ.cast("str", job(workflow, name)["if"])
        assert not suppresses_implicit_success(condition), name


def test_no_condition_accepts_a_skipped_prerequisite(
    workflow: dict[str, object],
) -> None:
    """A skipped prerequisite must stay a stop, not an accepted outcome."""
    for name in GUARDED_JOBS:
        declaration = job(workflow, name)
        condition = typ.cast("str", declaration.get("if", ""))
        for prerequisite in dependency_names(declaration):
            assert f"needs.{prerequisite}.result == 'skipped'" not in condition, (
                f"{name} accepts a skipped {prerequisite}"
            )


def test_a_dispatch_only_publishes_when_asked(
    workflow: dict[str, object],
) -> None:
    """A validation dispatch must not create a release."""
    condition = typ.cast("str", job(workflow, "publish")["if"])
    assert "github.event_name == 'schedule'" in condition
    assert "inputs.publish == true" in condition
