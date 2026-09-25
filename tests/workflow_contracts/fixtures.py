"""A compliant workflow tree for the rule tests, and a way to break it.

Every refusal case starts from this tree and changes one thing, and the
tree itself must pass every rule, so each case proves the rule refuses
the change rather than something the fixture already got wrong.
"""

from __future__ import annotations

import textwrap
import typing as typ

from .codescene_publisher import (
    concurrency_violations,
    find_publisher,
    permission_violations,
    retired_checksum_violations,
    trigger_violations,
    upload_step_violations,
)
from .codescene_reach import pull_request_closure, pull_request_violations
from .codescene_token import (
    check_step_violations,
    token_scope_violations,
    upload_guard_violations,
    upload_token_violations,
)
from .coverage_lanes import (
    publisher_lane_violations,
    pull_request_lane_violations,
    report_violations,
    second_writer_violations,
)
from .loading import Document, load_workflow

REPOSITORY: typ.Final[str] = "leynos/example"
PIN: typ.Final[str] = "a" * 40
SHARED: typ.Final[str] = "leynos/shared-actions/.github/actions"

PULL_REQUEST_LANE: typ.Final[str] = textwrap.dedent(f"""\
    name: CI
    on:
      push:
        branches: [main]
      pull_request:
    jobs:
      lint-test:
        runs-on: ubuntu-latest
        steps:
          - uses: actions/checkout@v4
          - name: Test and Measure Coverage
            if: github.event_name == 'pull_request'
            uses: {SHARED}/generate-coverage@{PIN}
            with:
              output-path: coverage.xml
              format: cobertura
              artefact-name-suffix: example
              with-ratchet: 'true'
              publish-artefact: 'false'
    """)

#: The availability check's one command, and the upload guard reading it.
CHECK_RUN: typ.Final[str] = (
    'echo "available=${{ secrets.CS_ACCESS_TOKEN != \'\' }}" >> "$GITHUB_OUTPUT"'
)
UPLOAD_IF: typ.Final[str] = (
    "steps.codescene-token.outputs.available == 'true'"
    " && github.ref == 'refs/heads/main'"
)

PUBLISHER: typ.Final[str] = textwrap.dedent(f"""\
    name: Coverage (main)
    on:
      push:
        branches: [main]
      workflow_dispatch:
    permissions: {{}}
    concurrency:
      group: coverage-main-${{{{ github.ref }}}}
      cancel-in-progress: false
    jobs:
      coverage-upload:
        runs-on: ubuntu-latest
        permissions:
          contents: read
        steps:
          - uses: actions/checkout@v4
          - name: Generate coverage
            uses: {SHARED}/generate-coverage@{PIN}
            with:
              output-path: coverage.xml
              format: cobertura
              artefact-name-suffix: example
              with-ratchet: 'true'
          - name: Check for the CodeScene token
            id: codescene-token
            run: {CHECK_RUN}
          - name: Upload coverage data to CodeScene
            if: {UPLOAD_IF}
            uses: {SHARED}/upload-codescene-coverage@{PIN}
            with:
              path: coverage.xml
              format: cobertura
              mode: upload
              access-token: ${{{{ secrets.CS_ACCESS_TOKEN }}}}
    """)

TREE: typ.Final[dict[str, str]] = {
    "ci.yml": PULL_REQUEST_LANE,
    "coverage-main.yml": PUBLISHER,
}


def tree(*, extra: dict[str, str] | None = None, **replaced: str) -> dict[str, str]:
    """Return the compliant tree's texts with files replaced or added.

    A keyword names a file by its stem (`ci`, `coverage_main`).
    """
    texts = dict(TREE)
    for stem, text in replaced.items():
        texts[f"{stem.replace('_', '-')}.yml"] = text
    return texts | (extra or {})


def replaced(text: str, old: str, new: str) -> str:
    """Return a text with one substitution applied, refusing a no-op.

    Raises
    ------
    ValueError
        If the text to replace is absent, since a mutation that changes
        nothing would pass for a reason that proves nothing.

    """
    result = text.replace(old, new)
    if result == text:
        message = f"replacing {old!r} with {new!r} would change nothing"
        raise ValueError(message)
    return result


def mutate(name: str, old: str, new: str) -> dict[str, str]:
    """Return the compliant tree with one exact substitution in one file.

    Raises
    ------
    ValueError
        If the text to replace is absent, since a mutation that changes
        nothing would pass for a reason that proves nothing.

    """
    return tree() | {name: replaced(TREE[name], old, new)}


def violations(texts: dict[str, str]) -> list[str]:
    """Return every CV-005 finding over a tree of workflow texts.

    Raises
    ------
    WorkflowReadingError
        If a workflow cannot be read, or the tree's shape defeats a
        reading, such as a second publisher.

    """
    documents: dict[str, Document] = {
        name: load_workflow(text) for name, text in texts.items()
    }
    closure = pull_request_closure(documents, REPOSITORY)
    name, publisher = find_publisher(documents)
    return [
        *pull_request_violations(documents, REPOSITORY),
        *trigger_violations(publisher),
        *concurrency_violations(publisher),
        *upload_step_violations(publisher),
        *permission_violations(publisher),
        *report_violations(publisher),
        *check_step_violations(publisher),
        *upload_guard_violations(publisher),
        *upload_token_violations(publisher),
        *token_scope_violations(publisher),
        *retired_checksum_violations(documents),
        *pull_request_lane_violations(closure),
        *second_writer_violations(documents, name, REPOSITORY),
        *publisher_lane_violations(publisher, closure),
    ]
