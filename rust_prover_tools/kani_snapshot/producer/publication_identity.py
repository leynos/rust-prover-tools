"""Choose the immutable publication identity a producer run may use.

The month and the upstream prefix already bind a tag to one upstream commit.
What remains is the revision: a run whose declared build inputs match the
newest existing revision may reuse it — resuming its draft when it is the same
run and attempt, or answering with a verified no-op when it is published — and
any other run takes the next unused revision. The inputs are compared by the
fingerprint recorded on the release, not by the builder commit alone, so a
change to the recipe or the toolchain gets its own identity. A look-alike tag
under the prefix is an error rather than something to skip, because skipping it
would publish a second "r1".

A manual dispatch supplies its identity instead of allocating one, so the same
grammar and upstream binding are checked without consulting existing releases.
Allocation reads untrusted release metadata, so the document it consumes is
parsed strictly and only the fields it needs are read.
"""

from __future__ import annotations

import dataclasses as dc
import re
import typing as typ

from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_snapshot.policy import reject_unknown_keys
from rust_prover_tools.kani_snapshot.producer.publication_document import (
    PUBLICATION_IDENTITY_SCHEMA_VERSION,
    PublicationIdentity,
    parse_publication_identity,
    publication_identity_to_json,
)
from rust_prover_tools.kani_snapshot.release_model import (
    parse_input_marker,
    parse_run_marker,
)
from rust_prover_tools.kani_snapshot.strict_json import strict_json_loads
from rust_prover_tools.kani_snapshot.validation import (
    require_full_sha,
    require_mapping,
    require_sequence,
    require_sha256,
    require_text,
    require_truthy_text,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from rust_prover_tools.kani_snapshot.models import WorkflowIdentity

__all__ = (
    "PUBLICATION_IDENTITY_SCHEMA_VERSION",
    "AllocationRequest",
    "PublicationIdentity",
    "PublicationRef",
    "allocate_publication_id",
    "parse_publication_identity",
    "parse_publications",
    "publication_identity_to_json",
    "publication_month",
    "publication_prefix",
)

#: Publication month shape, `YYYY-MM`, as the immutable tag grammar records it.
_MONTH: typ.Final[re.Pattern[str]] = re.compile(r"\d{4}-(?:0[1-9]|1[0-2])")

#: Revision suffix of an existing tag for the same month and upstream prefix.
_REVISION: typ.Final[re.Pattern[str]] = re.compile(r"-r(?P<revision>[1-9]\d*)")

#: Tag shape, repeated here only to recover the month of a manual identity.
_TAG_MONTH: typ.Final[re.Pattern[str]] = re.compile(
    r"kani-snapshot-(?P<month>\d{4}-(?:0[1-9]|1[0-2]))-[0-9a-f]{12}-r[1-9]\d*"
)

_RELEASE_KEYS: typ.Final[frozenset[str]] = frozenset({
    "tag",
    "builder_commit",
    "draft",
    "body",
})


@dc.dataclass(frozen=True, slots=True)
class PublicationRef:
    """One existing release a scheduled run must not collide with.

    Attributes
    ----------
    tag : str
        Published or drafted snapshot tag.
    builder_commit : str
        Commit the release records as its builder target.
    is_draft : bool
        Whether the release is still a draft, whose bytes are incomplete.
    body : str | None
        Release body, which records the build-input fingerprint and, for a
        draft, the run and attempt that created it.
    """

    tag: str
    builder_commit: str
    is_draft: bool
    body: str | None = None


def publication_prefix(upstream_commit: str, month: str) -> str:
    """Return the tag stem shared by every revision of one month and commit.

    Parameters
    ----------
    upstream_commit : str
        Full upstream commit the snapshot is built from.
    month : str
        Publication month in UTC, `YYYY-MM`.

    Returns
    -------
    str
        `kani-snapshot-YYYY-MM-<first 12 hex of the commit>`.

    Raises
    ------
    ProverToolError
        Raised when the commit is not a full lowercase SHA or the month is not
        a calendar month.
    """
    commit = require_full_sha(upstream_commit, "publication upstream commit")
    if _MONTH.fullmatch(require_truthy_text(month, "publication month")) is None:
        msg = f"publication month must be YYYY-MM, got {month!r}"
        raise ProverToolError(msg)
    return f"kani-snapshot-{month}-{commit[:12]}"


def publication_month(tag: str) -> str:
    """Return the month a manual identity's tag records.

    Parameters
    ----------
    tag : str
        Tag already accepted by the immutable grammar.

    Returns
    -------
    str
        `YYYY-MM` from the tag.

    Raises
    ------
    ProverToolError
        Raised when the tag is not in the immutable grammar.
    """
    match = _TAG_MONTH.fullmatch(require_truthy_text(tag, "publication tag"))
    if match is None:
        msg = f"publication tag {tag!r} is not in the immutable grammar"
        raise ProverToolError(msg)
    return match.group("month")


def _revision(prefix: str, tag: str) -> int:
    """Return a sibling tag's revision, refusing a look-alike name."""
    suffix = tag.removeprefix(f"{prefix}")
    match = _REVISION.fullmatch(suffix)
    if match is None:
        msg = (
            f"existing tag {tag!r} shares the prefix {prefix!r} without a "
            "revision suffix"
        )
        raise ProverToolError(msg)
    return int(match.group("revision"))


def _reusable(
    newest: PublicationRef,
    *,
    fingerprint: str,
    workflow: WorkflowIdentity,
) -> bool:
    """Return whether this run may reuse the newest existing revision.

    A published release may answer with a no-op only when it recorded exactly
    these build inputs. A draft may be resumed only by the run and attempt that
    created it; a later attempt, or a draft from other inputs, needs its own
    revision because only that attempt holds the validation evidence the
    draft's uploads were authorised by.
    """
    if parse_input_marker(newest.body) != fingerprint:
        return False
    if not newest.is_draft:
        return True
    return parse_run_marker(newest.body) == (workflow.run_id, workflow.run_attempt)


@dc.dataclass(frozen=True, slots=True)
class AllocationRequest:
    """Everything allocation needs to choose this run's revision.

    The values are grouped so the allocator takes one argument and each caller
    names every input it supplies.

    Attributes
    ----------
    upstream_commit : str
        Full upstream commit the snapshot is built from.
    month : str
        Publication month in UTC, `YYYY-MM`.
    builder_commit : str
        Trusted builder commit of this run.
    input_fingerprint : str
        Digest of the declared build inputs this run would build.
    workflow : WorkflowIdentity
        Run allocating the identity, which a draft must match to resume.
    releases : collections.abc.Sequence of PublicationRef
        Existing releases, in any order.
    """

    upstream_commit: str
    month: str
    builder_commit: str
    input_fingerprint: str
    workflow: WorkflowIdentity
    releases: cabc.Sequence[PublicationRef]


def allocate_publication_id(request: AllocationRequest) -> str:
    """Choose the immutable identity this run may publish under.

    The newest revision of the month and prefix is reused when its recorded
    build inputs equal this run's, which lets an interrupted draft resume and
    lets an already published release answer with a verified no-op. Any other
    case — changed inputs, a draft from another attempt, or a release with no
    recorded inputs — takes the next unused revision.

    Parameters
    ----------
    request : AllocationRequest
        Resolved commit, month, builder, inputs, workflow and existing
        releases.

    Returns
    -------
    str
        The immutable tag to build and publish.

    Raises
    ------
    ProverToolError
        Raised when an identity is malformed or a prefix look-alike exists.
    """
    prefix = publication_prefix(request.upstream_commit, request.month)
    require_full_sha(request.builder_commit, "publication builder commit")
    fingerprint = require_sha256(
        request.input_fingerprint,
        "publication input fingerprint",
    )
    revisions: dict[int, PublicationRef] = {}
    for release in request.releases:
        if not release.tag.startswith(f"{prefix}-"):
            continue
        revision = _revision(prefix, release.tag)
        if revision in revisions:
            msg = f"publication identity {release.tag!r} exists more than once"
            raise ProverToolError(msg)
        revisions[revision] = release
    if not revisions:
        return f"{prefix}-r1"
    newest = revisions[max(revisions)]
    if _reusable(newest, fingerprint=fingerprint, workflow=request.workflow):
        return newest.tag
    return f"{prefix}-r{max(revisions) + 1}"


def _require_complete(record: dict[str, object], field: str) -> None:
    """Require the release-list keys to be exactly the documented set."""
    reject_unknown_keys(record, _RELEASE_KEYS, field)
    missing = sorted(_RELEASE_KEYS - set(record))
    if missing:
        msg = f"{field} is missing keys: {', '.join(missing)}"
        raise ProverToolError(msg)


def _require_draft(value: object, field: str) -> bool:
    """Require the `draft` flag to be a boolean."""
    if not isinstance(value, bool):
        msg = f"{field} draft must be a boolean"
        raise ProverToolError(msg)
    return value


def _optional_body(value: object, field: str) -> str | None:
    """Require `body` to be a string or null."""
    if value is None:
        return None
    if not isinstance(value, str):
        msg = f"{field} body must be a string or null"
        raise ProverToolError(msg)
    return value


def _publication_ref(item: object, field: str) -> PublicationRef:
    """Parse one release-list member into the identity the allocator reads."""
    record = require_mapping(item, field)
    _require_complete(record, field)
    return PublicationRef(
        tag=require_truthy_text(record["tag"], f"{field} tag"),
        builder_commit=require_text(
            record["builder_commit"], f"{field} builder_commit"
        ),
        is_draft=_require_draft(record["draft"], field),
        body=_optional_body(record["body"], field),
    )


def parse_publications(data: bytes) -> tuple[PublicationRef, ...]:
    """Parse the release list collected by the resolve job.

    Parameters
    ----------
    data : bytes
        Exact document bytes: a JSON array of
        `{tag, builder_commit, draft, body}` objects.

    Returns
    -------
    tuple[PublicationRef, ...]
        Observed releases, in document order.

    Raises
    ------
    ProverToolError
        Raised for invalid JSON, a non-array document, an unknown or missing
        key, or a member that is not a string.
    """
    items = require_sequence(strict_json_loads(data, "release list"), "release list")
    releases = [
        _publication_ref(item, f"release {index}") for index, item in enumerate(items)
    ]
    return tuple(releases)
