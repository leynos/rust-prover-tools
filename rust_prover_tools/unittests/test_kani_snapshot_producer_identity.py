"""A scheduled run chooses one immutable identity and never silently renames it.

The month and upstream prefix already bind a tag to one upstream commit. What
remains is the revision: a run that records the builder commit of the newest
revision may resume or answer with a no-op, and any other run takes the next
unused revision. A look-alike tag is an error rather than something to skip,
because skipping it would publish a second "r1".
"""

from __future__ import annotations

import json

import pytest

from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_snapshot.producer.publication_identity import (
    PUBLICATION_IDENTITY_SCHEMA_VERSION,
    PublicationIdentity,
    PublicationRef,
    allocate_publication_id,
    parse_publication_identity,
    parse_publications,
    publication_identity_to_json,
    publication_month,
    publication_prefix,
)
from rust_prover_tools.unittests import kani_producer_fixtures as fixtures
from rust_prover_tools.unittests.kani_producer_fixtures import UPSTREAM_COMMIT

MONTH = "2026-11"
BUILDER = "b" * 40
OTHER_BUILDER = "c" * 40
PREFIX = f"kani-snapshot-{MONTH}-{UPSTREAM_COMMIT[:12]}"


def _allocate(
    *releases: PublicationRef,
    builder_commit: str = BUILDER,
) -> str:
    """Allocate an identity for the pinned commit and builder."""
    return allocate_publication_id(
        upstream_commit=UPSTREAM_COMMIT,
        month=MONTH,
        builder_commit=builder_commit,
        releases=releases,
    )


def test_the_prefix_names_the_month_and_twelve_hex_of_the_commit() -> None:
    """One prefix identifies one upstream commit for one month."""
    assert publication_prefix(UPSTREAM_COMMIT, MONTH) == PREFIX


def test_the_first_publication_of_a_month_is_revision_one() -> None:
    """With no sibling, the next unused revision is one."""
    assert _allocate() == f"{PREFIX}-r1"


def test_unrelated_tags_do_not_advance_the_revision() -> None:
    """A tag outside the prefix says nothing about this identity."""
    releases = (
        PublicationRef("kani-snapshot-2026-10-aaaaaaaaaaaa-r9", BUILDER),
        PublicationRef("v1.2.3", BUILDER),
    )
    assert _allocate(*releases) == f"{PREFIX}-r1"


def test_the_same_builder_commit_resumes_its_newest_revision() -> None:
    """An interrupted draft or a published release keeps its identity."""
    releases = (
        PublicationRef(f"{PREFIX}-r1", OTHER_BUILDER),
        PublicationRef(f"{PREFIX}-r2", BUILDER),
    )
    assert _allocate(*releases) == f"{PREFIX}-r2"


def test_a_new_builder_commit_takes_the_next_revision() -> None:
    """Changed inputs never rewrite a revision that already exists."""
    releases = (
        PublicationRef(f"{PREFIX}-r1", OTHER_BUILDER),
        PublicationRef(f"{PREFIX}-r2", OTHER_BUILDER),
    )
    assert _allocate(*releases) == f"{PREFIX}-r3"


def test_a_look_alike_tag_is_refused() -> None:
    """A tag under the prefix without a revision is ambiguous, not ignorable."""
    releases = (PublicationRef(f"{PREFIX}-latest", BUILDER),)

    with pytest.raises(ProverToolError, match="without a revision suffix"):
        _allocate(*releases)


def test_a_repeated_revision_is_refused() -> None:
    """Two releases claiming r1 cannot both be the one to resume."""
    releases = (
        PublicationRef(f"{PREFIX}-r1", BUILDER),
        PublicationRef(f"{PREFIX}-r1", OTHER_BUILDER),
    )

    with pytest.raises(ProverToolError, match="exists more than once"):
        _allocate(*releases)


def test_the_month_must_be_a_calendar_month() -> None:
    """A free-form month would escape the immutable tag grammar."""
    with pytest.raises(ProverToolError, match="YYYY-MM"):
        publication_prefix(UPSTREAM_COMMIT, "2026-13")


def test_the_upstream_commit_must_be_a_full_sha() -> None:
    """A ref or an abbreviation cannot identify an upstream build input."""
    with pytest.raises(ProverToolError, match="upstream commit"):
        publication_prefix("main", MONTH)


def test_the_builder_commit_must_be_a_full_sha() -> None:
    """An abbreviated builder commit could collide across revisions."""
    with pytest.raises(ProverToolError, match="builder commit"):
        _allocate(builder_commit="abc1234")


def test_the_release_document_is_parsed_strictly() -> None:
    """Untrusted release metadata is read through an exact key set."""
    data = json.dumps([
        {"tag": f"{PREFIX}-r1", "builder_commit": BUILDER},
        {"tag": "v1.2.3", "builder_commit": ""},
    ]).encode("utf-8")

    assert parse_publications(data) == (
        PublicationRef(f"{PREFIX}-r1", BUILDER),
        PublicationRef("v1.2.3", ""),
    )


@pytest.mark.parametrize(
    "document",
    [
        b"{}",
        b'[{"tag": "x"}]',
        b'[{"tag": "x", "builder_commit": "", "extra": 1}]',
        b'[{"tag": 1, "builder_commit": ""}]',
    ],
    ids=["not-a-list", "missing-key", "unknown-key", "wrong-type"],
)
def test_a_malformed_release_document_is_refused(document: bytes) -> None:
    """A shape the workflow did not intend fails closed, not silently."""
    with pytest.raises(ProverToolError):
        parse_publications(document)


def test_the_month_is_recovered_from_a_manual_tag() -> None:
    """A dispatched identity carries its own month."""
    assert publication_month(f"{PREFIX}-r7") == MONTH


def test_a_manual_tag_outside_the_grammar_has_no_month() -> None:
    """Anything the tag grammar rejects cannot supply a month."""
    with pytest.raises(ProverToolError, match="immutable grammar"):
        publication_month("kani-snapshot-2026-13-aaaaaaaaaaaa-r1")


def test_the_identity_document_round_trips() -> None:
    """The build job reads exactly the identity the resolve job wrote."""
    identity = PublicationIdentity(
        schema_version=PUBLICATION_IDENTITY_SCHEMA_VERSION,
        tag=f"{PREFIX}-r2",
        month=MONTH,
        upstream_commit=UPSTREAM_COMMIT,
        builder_commit=BUILDER,
        workflow=fixtures.workflow(),
    )

    document = publication_identity_to_json(identity)
    assert parse_publication_identity(document) == identity


def test_an_identity_with_an_unknown_schema_is_refused() -> None:
    """A version bump cannot be read by a producer that predates it."""
    document = json.loads(
        publication_identity_to_json(
            PublicationIdentity(
                schema_version=PUBLICATION_IDENTITY_SCHEMA_VERSION,
                tag=f"{PREFIX}-r1",
                month=MONTH,
                upstream_commit=UPSTREAM_COMMIT,
                builder_commit=BUILDER,
                workflow=fixtures.workflow(),
            )
        )
    )
    document["schema_version"] = 99

    with pytest.raises(ProverToolError, match="schema_version"):
        parse_publication_identity(json.dumps(document).encode("utf-8"))


def test_an_identity_tag_must_name_its_upstream_commit() -> None:
    """A tag from another upstream cannot be adopted for this build."""
    document = json.loads(
        publication_identity_to_json(
            PublicationIdentity(
                schema_version=PUBLICATION_IDENTITY_SCHEMA_VERSION,
                tag=f"{PREFIX}-r1",
                month=MONTH,
                upstream_commit=UPSTREAM_COMMIT,
                builder_commit=BUILDER,
                workflow=fixtures.workflow(),
            )
        )
    )
    document["tag"] = "kani-snapshot-2026-11-ffffffffffff-r1"

    with pytest.raises(ProverToolError, match="prefix"):
        parse_publication_identity(json.dumps(document).encode("utf-8"))
