"""The CV-005 contract over this repository's own workflows.

Main owns CodeScene: one push-to-main publisher uploads coverage and
writes the ratchet baseline, and nothing a pull request can start talks
to CodeScene or holds its credential. The rule tests beside this module
prove each clause refuses the shape it exists to refuse.
"""

from __future__ import annotations

import typing as typ
from pathlib import Path

import pytest

from .codescene_publisher import (
    concurrency_violations,
    find_publisher,
    retired_checksum_violations,
    token_scope_violations,
    trigger_violations,
    upload_step_violations,
)
from .codescene_reach import pull_request_closure, pull_request_violations
from .coverage_lanes import (
    publisher_lane_violations,
    pull_request_lane_violations,
    second_writer_violations,
)
from .loading import Document, read_workflows

REPOSITORY: typ.Final[str] = "leynos/rust-prover-tools"
ROOT: typ.Final[Path] = Path(__file__).resolve().parents[2]
WORKFLOWS: typ.Final[Path] = ROOT / ".github" / "workflows"
PUBLISHER: typ.Final[str] = "coverage-main.yml"

# mutmut copies the tests into a `mutants/` sandbox without `.github/`.
# Anywhere else a missing directory is the reader failing, and
# `read_workflows` raises rather than letting every rule pass over nothing.
pytestmark = pytest.mark.skipif(
    ROOT.name == "mutants" and not WORKFLOWS.exists(),
    reason="inside mutmut's sandbox, which does not copy .github/",
)


@pytest.fixture(scope="module")
def documents() -> dict[str, Document]:
    """Return this repository's workflows, parsed strictly."""
    return read_workflows(WORKFLOWS)


@pytest.fixture(scope="module")
def publisher(documents: dict[str, Document]) -> Document:
    """Return the one workflow that contacts CodeScene."""
    name, document = find_publisher(documents)
    assert name == PUBLISHER, name
    return document


def test_nothing_a_pull_request_starts_reaches_codescene(
    documents: dict[str, Document],
) -> None:
    """The pull-request closure names no host, credential, client or uploader."""
    found = pull_request_violations(documents, REPOSITORY)
    assert not found, found


def test_the_publisher_runs_only_on_a_push_to_main(publisher: Document) -> None:
    """The publisher answers a push to main and an optional dispatch only."""
    found = trigger_violations(publisher)
    assert not found, found


def test_the_publisher_never_cancels(publisher: Document) -> None:
    """A newer push replaces a pending run; no running upload is cancelled."""
    found = concurrency_violations(publisher)
    assert not found, found


def test_the_upload_is_guarded_and_bound(publisher: Document) -> None:
    """The upload step carries the ref guard and binds the token positively."""
    found = upload_step_violations(publisher)
    assert not found, found


def test_the_token_reaches_only_the_upload_step(publisher: Document) -> None:
    """No wider scope or other step of the publisher holds the credential."""
    found = token_scope_violations(publisher)
    assert not found, found


def test_the_retired_checksum_is_gone(documents: dict[str, Document]) -> None:
    """No workflow names the installer checksum or refreshes it."""
    found = retired_checksum_violations(documents)
    assert not found, found


def test_pull_request_lanes_ratchet_without_publishing(
    documents: dict[str, Document],
) -> None:
    """Every pull-request coverage lane ratchets and uploads no artefact."""
    closure = pull_request_closure(documents, REPOSITORY)
    found = pull_request_lane_violations(closure)
    assert not found, found


def test_only_the_publisher_writes_the_baseline(
    documents: dict[str, Document],
) -> None:
    """Coverage elsewhere is guarded to pull requests, so main has one writer."""
    found = second_writer_violations(documents, PUBLISHER, REPOSITORY)
    assert not found, found


def test_the_publisher_measures_what_each_lane_measures(
    documents: dict[str, Document], publisher: Document
) -> None:
    """The baseline is taken over the selection and pin the lanes use."""
    closure = pull_request_closure(documents, REPOSITORY)
    found = publisher_lane_violations(publisher, closure)
    assert not found, found
