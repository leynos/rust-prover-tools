"""Refusal cases for the pull-request surface: nothing there reaches CodeScene.

Each case changes one thing in the compliant fixture tree and names the
rule that must refuse it, so deleting that rule's clause fails the case.
"""

from __future__ import annotations

import textwrap

import pytest

from .codescene_reach import local_callee, pull_request_violations
from .fixtures import REPOSITORY, is_refused, mutate, tree
from .loading import Document, WorkflowReadingError, load_workflow

#: A reusable workflow declaring only `workflow_call`, curling the
#: CodeScene project API with the credential it inherits.
CALLEE = textwrap.dedent("""\
    on:
      workflow_call:
    jobs:
      probe:
        runs-on: ubuntu-latest
        steps:
          - run: 'curl -H "Authorization: $TOKEN" https://api.codescene.io/v2/projects'
            env:
              TOKEN: ${{ secrets.CS_ACCESS_TOKEN }}
    """)

#: The credential as a workflow expression reads it.
REFERENCE = "${{ secrets.CS_ACCESS_TOKEN }}"

#: A step that curls the CodeScene host, for the trigger-form cases.
CURL_JOB = "jobs:\n  a:\n    runs-on: x\n    steps:\n      - run: curl codescene.io\n"

#: A pull-request job calling a local workflow and forwarding everything.
CALLER = textwrap.dedent("""\
    on: [pull_request]
    jobs:
      call:
        uses: {spelling}.github/workflows/{callee}
        secrets: inherit
    """)


def _documents(texts: dict[str, str]) -> dict[str, Document]:
    """Parse a tree of texts."""
    return {name: load_workflow(text) for name, text in texts.items()}


def _findings(texts: dict[str, str]) -> list[str]:
    """Return the pull-request surface findings over a tree."""
    return pull_request_violations(_documents(texts), REPOSITORY)


def test_the_compliant_tree_passes_every_rule() -> None:
    """The fixture the refusal cases start from is itself compliant."""
    assert not is_refused(tree())


@pytest.mark.parametrize("spelling", ["./", "$/"])
def test_a_called_workflow_is_inside_the_closure(spelling: str) -> None:
    """A `workflow_call` callee reached through either local spelling is read."""
    texts = tree(
        extra={
            "probe.yml": CALLER.format(spelling=spelling, callee="callee.yml"),
            "callee.yml": CALLEE,
        }
    )
    findings = _findings(texts)
    assert any(item.startswith("callee.yml: names") for item in findings)
    assert "probe.yml: job call uses secrets: inherit" in findings


def test_the_closure_follows_a_chain_of_calls() -> None:
    """A callee two calls deep is reached, not only a direct one."""
    middle = textwrap.dedent("""\
        on:
          workflow_call:
        jobs:
          call:
            uses: ./.github/workflows/callee.yml
        """)
    texts = tree(
        extra={
            "probe.yml": CALLER.format(spelling="./", callee="middle.yml").replace(
                "    secrets: inherit\n", ""
            ),
            "middle.yml": middle,
            "callee.yml": CALLEE,
        }
    )
    assert any(item.startswith("callee.yml: names") for item in _findings(texts))


@pytest.mark.parametrize(
    ("where", "text"),
    [
        ("run body", "      - run: echo ${{ secrets.CS_ACCESS_TOKEN }}\n"),
        (
            "action input",
            f"      - uses: x/y@v1\n        with:\n          t: {REFERENCE}\n",
        ),
        (
            "renamed env",
            f"      - run: 'true'\n        env:\n          OTHER: {REFERENCE}\n",
        ),
        ("shell curl", "      - run: curl https://API.CodeScene.IO/v2\n"),
        ("client", "      - run: cs-coverage check coverage.xml\n"),
        (
            "uploader",
            "      - uses: o/r/.github/actions/upload-codescene-coverage@v1\n",
        ),
    ],
)
def test_a_pull_request_step_cannot_reach_codescene(where: str, text: str) -> None:
    """The credential, host, client or uploader in any step is refused."""
    texts = mutate("ci.yml", "      - uses: actions/checkout@v4\n", text)
    assert _findings(texts), where


def test_named_secret_forwarding_is_refused() -> None:
    """A reusable call forwarding the credential by name is refused."""
    caller = textwrap.dedent("""\
        on: pull_request
        jobs:
          call:
            uses: leynos/shared-actions/.github/workflows/x.yml@main
            secrets:
              token: ${{ secrets.CS_ACCESS_TOKEN }}
        """)
    assert _findings(tree(extra={"probe.yml": caller}))


def test_workflow_defaults_and_secret_declarations_are_read() -> None:
    """`defaults.run.shell` and a `workflow_call` secret key are both scanned."""
    shell = "defaults:\n  run:\n    shell: bash -c 'curl codescene.io; bash {0}'\njobs:"
    declared = (
        CALLEE
        .replace(
            "  workflow_call:\n",
            "  workflow_call:\n    secrets:\n      CS_ACCESS_TOKEN:\n",
        )
        .replace("https://api.codescene.io/v2/projects", "example.org")
        .replace("${{ secrets.CS_ACCESS_TOKEN }}", "x")
    )
    texts = mutate("ci.yml", "jobs:", shell) | {
        "probe.yml": CALLER.format(spelling="./", callee="callee.yml"),
        "callee.yml": declared,
    }
    findings = _findings(texts)
    assert any(item.startswith("ci.yml: names") for item in findings)
    assert any(
        item.startswith("callee.yml: names 'CS_ACCESS_TOKEN'") for item in findings
    )


@pytest.mark.parametrize(
    "form",
    [
        "on: pull_request\n",
        "on: [push, pull_request]\n",
        "'on':\n  pull_request_target:\n",
    ],
)
def test_every_trigger_form_seeds_the_closure(form: str) -> None:
    """Scalar, sequence and mapping trigger forms all start the closure."""
    probe = form + CURL_JOB
    assert any(
        item.startswith("probe.yml")
        for item in _findings(tree(extra={"probe.yml": probe}))
    )


def test_a_workflow_run_chain_is_inside_the_closure() -> None:
    """A workflow chained onto another's run is treated as reachable."""
    probe = "on:\n  workflow_run:\n    workflows: [CI]\n" + CURL_JOB
    assert any(
        item.startswith("probe.yml")
        for item in _findings(tree(extra={"probe.yml": probe}))
    )


@pytest.mark.parametrize(
    "reference",
    [
        "leynos/example/.github/workflows/callee.yml@main",
        "LEYNOS/Example/.github/workflows/callee.yml@v1",
        "$/.github/workflows/callee.yml@main",
    ],
)
def test_a_call_this_checkout_cannot_read_is_refused(reference: str) -> None:
    """A self-call at a ref runs a file this checkout does not hold."""
    with pytest.raises(WorkflowReadingError):
        local_callee(reference, REPOSITORY)


def test_a_call_to_a_missing_local_workflow_is_refused() -> None:
    """A closure naming a file the tree lacks is the reader failing."""
    texts = tree(extra={"probe.yml": CALLER.format(spelling="./", callee="absent.yml")})
    with pytest.raises(WorkflowReadingError, match="does not exist"):
        _findings(texts)


def test_another_repository_is_not_followed() -> None:
    """A cross-repository call is out of this tree and is not a local callee."""
    assert local_callee("leynos/other/.github/workflows/x.yml@main", REPOSITORY) is None
