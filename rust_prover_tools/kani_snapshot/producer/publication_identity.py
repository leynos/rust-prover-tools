"""Choose the immutable publication identity a producer run may use.

The month and the upstream prefix already bind a tag to one upstream commit.
What remains is the revision: a run whose builder commit matches the newest
existing revision may resume it, and any other run takes the next unused
revision. A look-alike tag under the prefix is an error rather than something
to skip, because skipping it would publish a second "r1".

A manual dispatch supplies its identity instead of allocating one, so the same
grammar and upstream binding are checked without consulting existing releases.
Allocation reads untrusted release metadata, so the document it consumes is
parsed strictly and only the fields it needs are read.
"""

from __future__ import annotations

import dataclasses as dc
import json
import re
import typing as typ

from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_snapshot.manifest_groups import parse_workflow
from rust_prover_tools.kani_snapshot.policy import reject_unknown_keys
from rust_prover_tools.kani_snapshot.strict_json import strict_json_loads
from rust_prover_tools.kani_snapshot.tags import validate_tag
from rust_prover_tools.kani_snapshot.validation import (
    require_full_sha,
    require_int,
    require_mapping,
    require_sequence,
    require_text,
    require_truthy_text,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from rust_prover_tools.kani_snapshot.models import WorkflowIdentity

__all__ = (
    "PUBLICATION_IDENTITY_SCHEMA_VERSION",
    "PublicationIdentity",
    "PublicationRef",
    "allocate_publication_id",
    "parse_publication_identity",
    "parse_publications",
    "publication_identity_to_json",
    "publication_month",
    "publication_prefix",
)

#: Version of the identity document the resolve job hands to the build job.
PUBLICATION_IDENTITY_SCHEMA_VERSION: typ.Final[int] = 1

#: Publication month shape, `YYYY-MM`, as the immutable tag grammar records it.
_MONTH: typ.Final[re.Pattern[str]] = re.compile(r"\d{4}-(?:0[1-9]|1[0-2])")

#: Revision suffix of an existing tag for the same month and upstream prefix.
_REVISION: typ.Final[re.Pattern[str]] = re.compile(r"-r(?P<revision>[1-9]\d*)")

#: Tag shape, repeated here only to recover the month of a manual identity.
_TAG_MONTH: typ.Final[re.Pattern[str]] = re.compile(
    r"kani-snapshot-(?P<month>\d{4}-(?:0[1-9]|1[0-2]))-[0-9a-f]{12}-r[1-9]\d*"
)

_RELEASE_KEYS: typ.Final[frozenset[str]] = frozenset({"tag", "builder_commit"})
_IDENTITY_KEYS: typ.Final[frozenset[str]] = frozenset({
    "schema_version",
    "tag",
    "month",
    "upstream_commit",
    "builder_commit",
    "workflow",
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
    """

    tag: str
    builder_commit: str


@dc.dataclass(frozen=True, slots=True)
class PublicationIdentity:
    """The immutable identity one producer run builds and publishes.

    Attributes
    ----------
    schema_version : int
        Document version, so a reader refuses a shape it does not know.
    tag : str
        Immutable snapshot tag.
    month : str
        Publication month in UTC, `YYYY-MM`.
    upstream_commit : str
        Full upstream commit the snapshot is built from.
    builder_commit : str
        Trusted builder commit of this run.
    workflow : WorkflowIdentity
        Run that allocated the identity.
    """

    schema_version: int
    tag: str
    month: str
    upstream_commit: str
    builder_commit: str
    workflow: WorkflowIdentity


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


def allocate_publication_id(
    *,
    upstream_commit: str,
    month: str,
    builder_commit: str,
    releases: cabc.Sequence[PublicationRef],
) -> str:
    """Choose the immutable identity this run may publish under.

    The recipe, the lockfiles and the toolchain all come from the builder
    commit, so two runs with the same upstream commit and the same builder
    commit have the same build inputs. The newest revision that recorded this
    builder commit is reused, which lets an interrupted draft resume and lets
    an already published release answer with a no-op. Any other case takes the
    next unused revision of the month and prefix.

    Parameters
    ----------
    upstream_commit : str
        Full upstream commit the snapshot is built from.
    month : str
        Publication month in UTC, `YYYY-MM`.
    builder_commit : str
        Trusted builder commit of this run.
    releases : collections.abc.Sequence of PublicationRef
        Existing releases, in any order.

    Returns
    -------
    str
        The immutable tag to build and publish.

    Raises
    ------
    ProverToolError
        Raised when an identity is malformed or a prefix look-alike exists.
    """
    prefix = publication_prefix(upstream_commit, month)
    builder = require_full_sha(builder_commit, "publication builder commit")
    revisions: dict[int, PublicationRef] = {}
    for release in releases:
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
    if newest.builder_commit == builder:
        return newest.tag
    return f"{prefix}-r{max(revisions) + 1}"


def parse_publications(data: bytes) -> tuple[PublicationRef, ...]:
    """Parse the release list collected by the resolve job.

    Parameters
    ----------
    data : bytes
        Exact document bytes: a JSON array of `{tag, builder_commit}` objects.

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
    releases: list[PublicationRef] = []
    for index, item in enumerate(items):
        record = require_mapping(item, f"release {index}")
        reject_unknown_keys(record, _RELEASE_KEYS, f"release {index}")
        missing = sorted(_RELEASE_KEYS - set(record))
        if missing:
            msg = f"release {index} is missing keys: {', '.join(missing)}"
            raise ProverToolError(msg)
        releases.append(
            PublicationRef(
                tag=require_truthy_text(record["tag"], f"release {index} tag"),
                builder_commit=require_text(
                    record["builder_commit"], f"release {index} builder_commit"
                ),
            )
        )
    return tuple(releases)


def _workflow_document(workflow: WorkflowIdentity) -> dict[str, object]:
    """Render a workflow identity as JSON-compatible values."""
    return {
        "path": workflow.path,
        "event": workflow.event,
        "ref": workflow.ref,
        "run_id": workflow.run_id,
        "run_attempt": workflow.run_attempt,
        "run_url": workflow.run_url,
    }


def publication_identity_to_json(identity: PublicationIdentity) -> bytes:
    """Render a publication identity as deterministic JSON bytes."""
    document = {
        "schema_version": identity.schema_version,
        "tag": identity.tag,
        "month": identity.month,
        "upstream_commit": identity.upstream_commit,
        "builder_commit": identity.builder_commit,
        "workflow": _workflow_document(identity.workflow),
    }
    text = json.dumps(document, sort_keys=True, indent=2, ensure_ascii=True)
    return (text + "\n").encode("ascii")


def parse_publication_identity(data: bytes) -> PublicationIdentity:
    """Parse exact identity bytes into a validated record.

    Parameters
    ----------
    data : bytes
        Exact document bytes as written by `publication_identity_to_json`.

    Returns
    -------
    PublicationIdentity
        Validated identity.

    Raises
    ------
    ProverToolError
        Raised for invalid JSON, unknown or missing keys, an unsupported
        schema version, or an identity that is not in the tag grammar.
    """
    document = require_mapping(
        strict_json_loads(data, "publication identity"), "publication identity"
    )
    reject_unknown_keys(document, _IDENTITY_KEYS, "publication identity")
    missing = sorted(_IDENTITY_KEYS - set(document))
    if missing:
        msg = f"publication identity is missing keys: {', '.join(missing)}"
        raise ProverToolError(msg)
    version = require_int(
        document["schema_version"], "publication identity schema_version"
    )
    if version != PUBLICATION_IDENTITY_SCHEMA_VERSION:
        msg = (
            "publication identity schema_version must be "
            f"{PUBLICATION_IDENTITY_SCHEMA_VERSION}, got {version}"
        )
        raise ProverToolError(msg)
    upstream_commit = require_full_sha(
        document["upstream_commit"], "publication identity upstream_commit"
    )
    return PublicationIdentity(
        schema_version=version,
        tag=validate_tag(document["tag"], upstream_commit),
        month=require_truthy_text(document["month"], "publication identity month"),
        upstream_commit=upstream_commit,
        builder_commit=require_full_sha(
            document["builder_commit"], "publication identity builder_commit"
        ),
        workflow=parse_workflow(
            require_mapping(document["workflow"], "publication identity workflow"),
            "publication identity workflow",
        ),
    )
