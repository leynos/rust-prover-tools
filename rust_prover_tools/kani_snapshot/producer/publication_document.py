"""The identity document the resolve job hands to the build job.

Allocation chooses a tag; this module owns the document that records it,
together with the upstream commit, builder commit, build-input fingerprint and
workflow that allocated it. The document is parsed strictly, so a producer
that does not know the schema version, or that would build a different
identity than the one recorded, refuses it rather than guessing.
"""

from __future__ import annotations

import dataclasses as dc
import json
import typing as typ

from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_snapshot.manifest_groups import parse_workflow
from rust_prover_tools.kani_snapshot.policy import reject_unknown_keys
from rust_prover_tools.kani_snapshot.strict_json import strict_json_loads
from rust_prover_tools.kani_snapshot.tags import TAG_PATTERN, validate_tag
from rust_prover_tools.kani_snapshot.validation import (
    require_full_sha,
    require_int,
    require_mapping,
    require_sha256,
    require_truthy_text,
)

if typ.TYPE_CHECKING:
    from rust_prover_tools.kani_snapshot.models import WorkflowIdentity

__all__ = (
    "PUBLICATION_IDENTITY_SCHEMA_VERSION",
    "PublicationIdentity",
    "parse_publication_identity",
    "publication_identity_to_json",
)

#: Version of the identity document the resolve job hands to the build job.
#:
#: Version 2 records the build-input fingerprint that decides tag reuse.
PUBLICATION_IDENTITY_SCHEMA_VERSION: typ.Final[int] = 2

_IDENTITY_KEYS: typ.Final[frozenset[str]] = frozenset({
    "schema_version",
    "tag",
    "month",
    "upstream_commit",
    "builder_commit",
    "input_fingerprint",
    "workflow",
})


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
    input_fingerprint : str
        Digest of the declared build inputs, which decides whether an
        existing release may be reused.
    workflow : WorkflowIdentity
        Run that allocated the identity.
    """

    schema_version: int
    tag: str
    month: str
    upstream_commit: str
    builder_commit: str
    input_fingerprint: str
    workflow: WorkflowIdentity


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
        "input_fingerprint": identity.input_fingerprint,
        "workflow": _workflow_document(identity.workflow),
    }
    text = json.dumps(document, sort_keys=True, indent=2, ensure_ascii=True)
    return (text + "\n").encode("ascii")


def _tag_month(tag: str) -> str:
    """Return the `YYYY-MM` month a validated tag records.

    Parameters
    ----------
    tag : str
        Tag already accepted by `validate_tag`.

    Returns
    -------
    str
        The calendar month embedded in the tag.

    Raises
    ------
    ProverToolError
        Raised when the tag is not in the immutable grammar. Callers pass an
        already validated tag, so this is a defensive check.
    """
    match = TAG_PATTERN.fullmatch(tag)
    if match is None:  # pragma: no cover - callers pass a validated tag
        msg = f"publication tag {tag!r} is not in the immutable grammar"
        raise ProverToolError(msg)
    return f"{match.group('year')}-{match.group('month')}"


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
        schema version, an identity that is not in the tag grammar, or a
        recorded month that disagrees with the tag.
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
    tag = validate_tag(document["tag"], upstream_commit)
    month = require_truthy_text(document["month"], "publication identity month")
    if _tag_month(tag) != month:
        msg = f"publication identity month {month!r} does not match tag {tag!r}"
        raise ProverToolError(msg)
    return PublicationIdentity(
        schema_version=version,
        tag=tag,
        month=month,
        upstream_commit=upstream_commit,
        builder_commit=require_full_sha(
            document["builder_commit"], "publication identity builder_commit"
        ),
        input_fingerprint=require_sha256(
            document["input_fingerprint"], "publication identity input_fingerprint"
        ),
        workflow=parse_workflow(
            require_mapping(document["workflow"], "publication identity workflow"),
            "publication identity workflow",
        ),
    )
