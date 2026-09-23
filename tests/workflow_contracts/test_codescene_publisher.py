"""Refusal cases for the publisher and the coverage lanes.

Each case changes one thing in the compliant fixture tree and asserts on
the one rule that must refuse it, so deleting that rule's clause fails
the case.
"""

from __future__ import annotations

import pytest

from .codescene_publisher import (
    concurrency_violations,
    find_publisher,
    retired_checksum_violations,
    token_scope_violations,
    trigger_violations,
    upload_step_violations,
)
from .coverage_lanes import (
    publisher_lane_violations,
    pull_request_lane_violations,
    second_writer_violations,
)
from .fixtures import PUBLISHER, PULL_REQUEST_LANE, REPOSITORY, mutate, tree
from .loading import Document, WorkflowReadingError, load_workflow

GUARD = "if: env.CS_ACCESS_TOKEN != '' && github.ref == 'refs/heads/main'"


def _publisher(texts: dict[str, str]) -> Document:
    """Return the parsed publisher of a tree."""
    return load_workflow(texts["coverage-main.yml"])


def _documents(texts: dict[str, str]) -> dict[str, Document]:
    """Parse a tree of texts."""
    return {name: load_workflow(text) for name, text in texts.items()}


@pytest.mark.parametrize(
    "guard",
    [
        # Every required term stays whole; only the `||` refusal catches it.
        f"{GUARD} && github.actor != 'x' || github.event_name == 'workflow_dispatch'",
        "if: env.CS_ACCESS_TOKEN != ''",
        "if: github.ref == 'refs/heads/main'",
        "if: env.CS_ACCESS_TOKEN != '' && github.ref != 'refs/heads/main'",
    ],
)
def test_the_upload_guard_needs_both_terms_and_no_disjunction(guard: str) -> None:
    """The ref and token guard must hold as whole terms of a conjunction."""
    texts = mutate("coverage-main.yml", GUARD, guard)
    assert upload_step_violations(_publisher(texts))


@pytest.mark.parametrize(
    "guard",
    [
        f"{GUARD} && github.actor != 'x'",
        "if: ${{ github.ref == 'refs/heads/main' && env.CS_ACCESS_TOKEN != '' }}",
        f"{GUARD} && 'a||b' != ''",
    ],
)
def test_a_narrower_upload_guard_is_accepted(guard: str) -> None:
    """Extra terms, a wrapper and a quoted `||` do not trip the guard rule."""
    texts = mutate("coverage-main.yml", GUARD, guard)
    assert not upload_step_violations(_publisher(texts))


@pytest.mark.parametrize(
    ("old", "new"),
    [
        (
            "          CS_ACCESS_TOKEN: ${{ secrets.CS_ACCESS_TOKEN }}\n",
            "          OTHER: x\n",
        ),
        ("          access-token: ${{ env.CS_ACCESS_TOKEN }}\n", ""),
        ("          mode: upload\n", "          mode: check\n"),
        ("upload-codescene-coverage@" + "a" * 40, "upload-codescene-coverage@main"),
    ],
)
def test_the_upload_step_binds_the_token_positively(old: str, new: str) -> None:
    """A deleted binding, a missing input, check mode or a branch pin is refused."""
    texts = mutate("coverage-main.yml", old, new)
    assert upload_step_violations(_publisher(texts))


def test_the_token_is_refused_in_any_wider_scope() -> None:
    """The credential bound at job level reaches every step, so it is refused."""
    job = "    runs-on: ubuntu-latest\n"
    wider = job + "    env:\n      T: ${{ secrets.CS_ACCESS_TOKEN }}\n"
    texts = mutate("coverage-main.yml", job, wider)
    assert token_scope_violations(_publisher(texts))


@pytest.mark.parametrize("value", ["true", "${{ github.ref != 'refs/heads/main' }}"])
def test_the_publisher_never_cancels(value: str) -> None:
    """A publisher that may cancel a running upload is refused."""
    texts = mutate(
        "coverage-main.yml", "cancel-in-progress: false", f"cancel-in-progress: {value}"
    )
    assert concurrency_violations(_publisher(texts))


WORKFLOW_GROUP = "concurrency:\n  group: coverage-main\n  cancel-in-progress: false\n"
UPLOAD_JOB = "  coverage-upload:\n    runs-on: ubuntu-latest\n"


def test_the_publisher_needs_a_concurrency_group() -> None:
    """A publisher without any concurrency declaration is refused."""
    texts = mutate("coverage-main.yml", WORKFLOW_GROUP, "")
    assert concurrency_violations(_publisher(texts))


def test_a_group_on_another_job_does_not_cover_the_upload() -> None:
    """A group on an unrelated job leaves concurrent uploads possible."""
    helper = "  helper:\n    runs-on: x\n    concurrency: helper\n    steps: []\n"
    text = PUBLISHER.replace(WORKFLOW_GROUP, "").replace(
        UPLOAD_JOB, helper + UPLOAD_JOB
    )
    assert concurrency_violations(load_workflow(text))


def test_a_group_on_the_upload_job_is_accepted() -> None:
    """The upload job's own group governs the upload as well as a workflow one."""
    text = PUBLISHER.replace(WORKFLOW_GROUP, "").replace(
        UPLOAD_JOB, UPLOAD_JOB + "    concurrency: coverage-main\n"
    )
    assert not concurrency_violations(load_workflow(text))


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("    branches: [main]\n", "    branches: ['**']\n"),
        ("    branches: [main]\n", "    tags: ['v*']\n"),
        (
            "  workflow_dispatch:\n",
            "  workflow_dispatch:\n  schedule:\n    - cron: '0 0 * * *'\n",
        ),
    ],
)
def test_the_publisher_runs_only_on_a_push_to_main(old: str, new: str) -> None:
    """Any branch, a tag push or another trigger is refused."""
    texts = mutate("coverage-main.yml", old, new)
    assert trigger_violations(_publisher(texts))


def test_a_second_uploader_is_refused() -> None:
    """Two workflows contacting CodeScene cannot both be the publisher."""
    texts = tree(extra={"second.yml": PUBLISHER})
    with pytest.raises(WorkflowReadingError, match="exactly one workflow"):
        find_publisher(_documents(texts))


@pytest.mark.parametrize(
    "addition",
    [
        "          installer-checksum: ${{ vars.CODESCENE_CLI_SHA256 }}\n",
        "          archive-checksum: ${{ vars.CODESCENE_CLI_SHA256 }}\n",
    ],
)
def test_the_retired_checksum_is_refused(addition: str) -> None:
    """The installer checksum and its variable are gone for good."""
    texts = mutate(
        "coverage-main.yml",
        "          mode: upload\n",
        "          mode: upload\n" + addition,
    )
    assert retired_checksum_violations(_documents(texts))


def test_the_checksum_refresher_is_refused() -> None:
    """The workflow that refreshed the retired checksum must not return."""
    refresher = "on: workflow_dispatch\njobs:\n  a:\n    runs-on: x\n    steps: []\n"
    texts = tree(extra={"get-codescene-sha.yml": refresher})
    assert retired_checksum_violations(_documents(texts))


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("          with-ratchet: 'true'\n", ""),
        ("          publish-artefact: 'false'\n", ""),
    ],
)
def test_a_pull_request_lane_ratchets_and_publishes_nothing(old: str, new: str) -> None:
    """A lane without the ratchet, or publishing its report, is refused."""
    documents = _documents(mutate("ci.yml", old, new))
    assert pull_request_lane_violations({"ci.yml": documents["ci.yml"]})


@pytest.mark.parametrize(
    "guard",
    [
        "",
        "        if: always()\n",
        "        if: github.event_name == 'pull_request' || always()\n",
    ],
)
def test_a_push_lane_cannot_write_a_second_baseline(guard: str) -> None:
    """Coverage on a push outside the publisher is refused."""
    texts = mutate("ci.yml", "        if: github.event_name == 'pull_request'\n", guard)
    assert second_writer_violations(_documents(texts), "coverage-main.yml", REPOSITORY)


def test_a_push_lane_cannot_write_a_baseline_through_a_callee() -> None:
    """A push workflow's local callee runs on the push, so its coverage counts."""
    caller = "on: push\njobs:\n  call:\n    uses: ./.github/workflows/cov.yml\n"
    callee = PULL_REQUEST_LANE.replace(
        "on:\n  push:\n    branches: [main]\n  pull_request:\n",
        "on:\n  workflow_call:\n",
    ).replace("        if: github.event_name == 'pull_request'\n", "")
    documents = _documents(tree(extra={"caller.yml": caller, "cov.yml": callee}))
    found = second_writer_violations(documents, "coverage-main.yml", REPOSITORY)
    assert (
        "cov.yml: generate-coverage can run on a push; guard it to pull requests"
        in found
    )


@pytest.mark.parametrize(
    ("name", "old", "new"),
    [
        (
            "ci.yml",
            "          output-path: coverage.xml\n",
            "          output-path: other.xml\n",
        ),
        ("coverage-main.yml", "          with-ratchet: 'true'\n", ""),
        ("ci.yml", "generate-coverage@" + "a" * 40, "generate-coverage@" + "b" * 40),
    ],
)
def test_the_publisher_measures_what_each_lane_measures(
    name: str, old: str, new: str
) -> None:
    """A selection or pin differing from the publisher's is refused."""
    documents = _documents(mutate(name, old, new))
    closure = {"ci.yml": documents["ci.yml"]}
    assert publisher_lane_violations(documents["coverage-main.yml"], closure)
