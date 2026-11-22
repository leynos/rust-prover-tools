"""A scheduled run chooses one immutable identity and never silently renames it.

The month and upstream prefix already bind a tag to one upstream commit. What
remains is the revision: a run whose recorded build inputs match the newest
revision may resume its own draft or answer with a verified no-op, and any
other run takes the next unused revision. A look-alike tag is an error rather
than something to skip, because skipping it would publish a second "r1".
"""

from __future__ import annotations

import json

import pytest

from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_snapshot.producer.publication_identity import (
    PUBLICATION_IDENTITY_SCHEMA_VERSION,
    AllocationRequest,
    PublicationIdentity,
    PublicationRef,
    allocate_publication_id,
    parse_publication_identity,
    parse_publications,
    publication_identity_to_json,
    publication_month,
    publication_prefix,
)
from rust_prover_tools.kani_snapshot.release_model import release_body
from rust_prover_tools.unittests import kani_producer_fixtures as fixtures
from rust_prover_tools.unittests.kani_producer_fixtures import UPSTREAM_COMMIT

MONTH = "2026-11"
BUILDER = "b" * 40
OTHER_BUILDER = "c" * 40
FINGERPRINT = "a" * 64
OTHER_FINGERPRINT = "e" * 64
PREFIX = f"kani-snapshot-{MONTH}-{UPSTREAM_COMMIT[:12]}"


def _body(tag: str, *, attempt: int = 1) -> str:
    """Return a release body recording one run and input set."""
    return release_body(
        tag,
        FINGERPRINT,
        run_id=12345,
        run_attempt=attempt,
    )


def _ref(
    tag: str,
    builder_commit: str = BUILDER,
    *,
    is_draft: bool = False,
    attempt: int = 1,
) -> PublicationRef:
    """Return one existing release as the allocator observes it."""
    return PublicationRef(
        tag,
        builder_commit,
        is_draft=is_draft,
        body=_body(tag, attempt=attempt),
    )


def _allocate(
    *releases: PublicationRef,
    builder_commit: str = BUILDER,
    fingerprint: str = FINGERPRINT,
) -> str:
    """Allocate an identity for the pinned commit, inputs and builder."""
    return allocate_publication_id(
        AllocationRequest(
            upstream_commit=UPSTREAM_COMMIT,
            month=MONTH,
            builder_commit=builder_commit,
            input_fingerprint=fingerprint,
            workflow=fixtures.workflow(),
            releases=releases,
        )
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
        _ref("kani-snapshot-2026-10-aaaaaaaaaaaa-r9"),
        _ref("v1.2.3"),
    )
    assert _allocate(*releases) == f"{PREFIX}-r1"


def test_a_published_release_with_the_same_inputs_is_reused() -> None:
    """An unchanged input set answers with the release that already exists."""
    releases = (
        _ref(f"{PREFIX}-r1", OTHER_BUILDER),
        _ref(f"{PREFIX}-r2"),
    )
    assert _allocate(*releases) == f"{PREFIX}-r2"


def test_this_runs_own_draft_is_resumed() -> None:
    """An interrupted attempt may finish the draft it created."""
    releases = (_ref(f"{PREFIX}-r1", is_draft=True),)
    assert _allocate(*releases) == f"{PREFIX}-r1"


def test_another_attempts_draft_is_never_resumed() -> None:
    """Only the attempt holding the validation evidence may finish a draft."""
    releases = (_ref(f"{PREFIX}-r1", is_draft=True, attempt=2),)
    assert _allocate(*releases) == f"{PREFIX}-r2"


def test_changed_inputs_take_the_next_revision() -> None:
    """A changed fingerprint never rewrites an existing revision."""
    releases = (
        _ref(f"{PREFIX}-r1"),
        _ref(f"{PREFIX}-r2"),
    )
    assert _allocate(*releases, fingerprint=OTHER_FINGERPRINT) == f"{PREFIX}-r3"


def test_a_release_without_recorded_inputs_is_not_reused() -> None:
    """An older release that cannot name its inputs is not assumed equal."""
    releases = (PublicationRef(f"{PREFIX}-r1", BUILDER, is_draft=False),)
    assert _allocate(*releases) == f"{PREFIX}-r2"


def test_a_look_alike_tag_is_refused() -> None:
    """A tag under the prefix without a revision is ambiguous, not ignorable."""
    releases = (_ref(f"{PREFIX}-latest"),)

    with pytest.raises(ProverToolError, match="without a revision suffix"):
        _allocate(*releases)


def test_a_repeated_revision_is_refused() -> None:
    """Two releases claiming r1 cannot both be the one to resume."""
    releases = (
        _ref(f"{PREFIX}-r1"),
        _ref(f"{PREFIX}-r1", OTHER_BUILDER),
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


def test_the_input_fingerprint_must_be_a_sha256() -> None:
    """A short fingerprint could collide across different input sets."""
    with pytest.raises(ProverToolError, match="input fingerprint"):
        _allocate(fingerprint="abc1234")


def test_the_release_document_is_parsed_strictly() -> None:
    """Untrusted release metadata is read through an exact key set."""
    data = json.dumps([
        {
            "tag": f"{PREFIX}-r1",
            "builder_commit": BUILDER,
            "draft": True,
            "body": "recorded",
        },
        {
            "tag": "v1.2.3",
            "builder_commit": "",
            "draft": False,
            "body": None,
        },
    ]).encode("utf-8")

    assert parse_publications(data) == (
        PublicationRef(f"{PREFIX}-r1", BUILDER, is_draft=True, body="recorded"),
        PublicationRef("v1.2.3", "", is_draft=False, body=None),
    )


@pytest.mark.parametrize(
    "document",
    [
        b"{}",
        b'[{"tag": "x"}]',
        b'[{"tag":"x","builder_commit":"","draft":false,"body":null,"extra":1}]',
        b'[{"tag": 1, "builder_commit": "", "draft": false, "body": null}]',
        b'[{"tag": "x", "builder_commit": "", "draft": "yes", "body": null}]',
        b'[{"tag": "x", "builder_commit": "", "draft": false, "body": 7}]',
    ],
    ids=[
        "not-a-list",
        "missing-key",
        "unknown-key",
        "wrong-type",
        "draft-not-boolean",
        "body-not-text",
    ],
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


def test_a_unicode_digit_revision_is_not_allocated() -> None:
    """A Unicode digit must not be read as an ASCII revision.

    Python's `int()` accepts Unicode decimal digits, so a `-r1٢` sibling used
    to allocate revision 12 while `validate_tag` then refused the same tag.
    """
    releases = (_ref(f"{PREFIX}-r1٢"),)

    with pytest.raises(ProverToolError, match="without a revision suffix"):
        _allocate(*releases)


def test_a_unicode_digit_year_is_not_a_calendar_month() -> None:
    """A year spelled with Unicode digits is outside the ASCII grammar."""
    with pytest.raises(ProverToolError, match="immutable grammar"):
        publication_month("kani-snapshot-٢٠٢٦-09-aaaaaaaaaaaa-r1")

    with pytest.raises(ProverToolError, match="YYYY-MM"):
        publication_prefix(UPSTREAM_COMMIT, "٢٠٢٦-11")


def _identity(tag: str = f"{PREFIX}-r2") -> PublicationIdentity:
    """Return one valid identity for the pinned commit and builder."""
    return PublicationIdentity(
        schema_version=PUBLICATION_IDENTITY_SCHEMA_VERSION,
        tag=tag,
        month=MONTH,
        upstream_commit=UPSTREAM_COMMIT,
        builder_commit=BUILDER,
        input_fingerprint=FINGERPRINT,
        workflow=fixtures.workflow(),
    )


def test_the_identity_document_round_trips() -> None:
    """The build job reads exactly the identity the resolve job wrote."""
    identity = _identity()

    document = publication_identity_to_json(identity)
    assert parse_publication_identity(document) == identity


def test_an_identity_with_an_unknown_schema_is_refused() -> None:
    """A version bump cannot be read by a producer that predates it."""
    document = json.loads(publication_identity_to_json(_identity()))
    document["schema_version"] = 99

    with pytest.raises(ProverToolError, match="schema_version"):
        parse_publication_identity(json.dumps(document).encode("utf-8"))


def test_an_identity_tag_must_name_its_upstream_commit() -> None:
    """A tag from another upstream cannot be adopted for this build."""
    document = json.loads(publication_identity_to_json(_identity()))
    document["tag"] = "kani-snapshot-2026-11-ffffffffffff-r1"

    with pytest.raises(ProverToolError, match="prefix"):
        parse_publication_identity(json.dumps(document).encode("utf-8"))


def test_an_identity_month_must_match_its_tag() -> None:
    """A recorded month that disagrees with the tag names two identities."""
    document = json.loads(publication_identity_to_json(_identity()))
    document["month"] = "2026-10"

    with pytest.raises(ProverToolError, match="does not match tag"):
        parse_publication_identity(json.dumps(document).encode("utf-8"))
