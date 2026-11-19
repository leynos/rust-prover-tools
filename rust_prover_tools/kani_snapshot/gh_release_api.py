"""GitHub release adapter for the publication state machine.

`publish` owns the orchestration and `release_policy` owns the decisions; this
module only turns those decisions into `gh` argument vectors and reads the
resulting state back. The adapter is deliberately narrow: `GhReleaseApi` builds
argument vectors through `rust_prover_tools.commands` and classifies transport
failures as transient.

Reading state and verifying bytes are separate commands. `observe` performs
queries only, so a caller can tell that an observation cost no download.
`verify` is the one command that downloads, hashes and authenticates, and its
result is a `VerifiedAssets` record rather than an edited observation. That
split is what makes `ObservedAsset.sha256` evidence rather than a claim: the
digest that authorises a publish is computed locally from the bytes the release
serves, and only a verification asks for it.
"""

from __future__ import annotations

import dataclasses as dc
import logging
import typing as typ

from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_snapshot.gh_release_document import (
    decoded,
    parse_release,
)
from rust_prover_tools.kani_snapshot.gh_release_download import verify_assets
from rust_prover_tools.kani_snapshot.gh_transport import (
    TRANSFER_TIMEOUT_SECONDS,
    gh,
    gh_optional,
)
from rust_prover_tools.kani_snapshot.models import REPOSITORY
from rust_prover_tools.kani_snapshot.release_model import (
    ExpectedRelease,
    ObservedAsset,
    ObservedRelease,
    ReleaseAsset,
    VerifiedAssets,
    release_body,
)
from rust_prover_tools.kani_snapshot.validation import (
    require_full_sha,
    require_mapping,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

__all__ = ("GhReleaseApi",)

_LOGGER = logging.getLogger(__name__)

#: Releases requested per page when the tag endpoint cannot see a draft.
_RELEASE_PAGE_SIZE: typ.Final[int] = 100

#: Pages the draft lookup may read. Reaching it is a failure, not an absence.
_RELEASE_PAGE_LIMIT: typ.Final[int] = 10

#: How many annotated tag objects one tag may chain through before it is refused.
_TAG_PEEL_LIMIT: typ.Final[int] = 4

#: Fields every observation needs, from either release endpoint.
_RELEASE_QUERY: typ.Final[str] = (
    "{id, tag_name, draft, target_commitish, body, assets: "
    "[.assets[] | {id, name, size, digest}]}"
)


def _find_tag(records: list[object], tag: str) -> ObservedRelease | None:
    """Return the listed release whose tag matches, or `None` when absent."""
    for item in records:
        candidate = parse_release(item)
        if candidate.tag == tag:
            return candidate
    return None


def _object_document(document: object, field: str) -> tuple[str, str]:
    """Return the type and full SHA of one Git object reference."""
    record = require_mapping(document, field)
    kind = record.get("type")
    if not isinstance(kind, str):
        msg = f"{field} must report an object type"
        raise ProverToolError(msg)
    return kind, require_full_sha(record.get("sha"), f"{field} sha")


def _optional_object(path: str, field: str) -> tuple[str, str] | None:
    """Read one Git object reference, or `None` when it does not exist."""
    stdout = gh_optional(("gh", "api", path, "--jq", ".object"))
    if stdout is None:
        return None
    return _object_document(decoded(stdout), field)


def _absent(tag: str, tag_commit: str | None) -> ObservedRelease:
    """Return the absent state, refusing a tag that already points somewhere."""
    if tag_commit is None:
        return ObservedRelease(exists=False, is_draft=False)
    msg = (
        f"tag {tag!r} already addresses {tag_commit} but no release carries it; "
        "refusing to create a draft that would adopt the existing tag"
    )
    raise ProverToolError(msg)


class GhReleaseApi:
    """GitHub release adapter built on `gh` argument vectors.

    Parameters
    ----------
    repository : str
        `owner/name` of the publishing repository. Defaults to the one
        snapshot repository and is never read from untrusted input.
    """

    def __init__(self, repository: str = REPOSITORY) -> None:
        """Record the single repository this adapter may address."""
        if repository != REPOSITORY:
            msg = f"release adapter may only address {REPOSITORY}"
            raise ProverToolError(msg)
        self._repository = repository

    def observe(self, tag: str) -> ObservedRelease:
        """Return the release metadata without downloading any asset.

        A missing release is an observation, not an error. The release-by-tag
        endpoint is tried first; when it cannot see the tag, the bounded
        release list is searched, because that endpoint is the one that
        reports an existing draft. Any other permanent failure is refused so a
        publication cannot proceed blind.

        The tag ref is resolved to the commit it addresses, because release
        metadata's `target_commitish` is ignored when the tag already exists.
        An existing tag therefore decides what the release points at, and an
        annotated tag is peeled to its commit rather than reported as its own
        tag object. A tag with no release at all is refused: creating a draft
        would silently adopt it.

        A published release must resolve to a tag ref: GitHub creates the tag
        when a draft is published, so a published release whose tag cannot be
        resolved describes a state this run must not accept, and policy would
        otherwise reach a no-op without confirming any target. A draft has no
        tag yet by design, so its recorded `target_commitish` stands.

        The observation reports `provenance_verified=False`: a state query
        cannot authenticate anything, and only `verify` may claim it did. A
        digest the API volunteers is reported as metadata, never as evidence.
        """
        observed = self._observe_by_tag(tag)
        if observed is None:
            observed = self._observe_by_list(tag)
        tag_commit = self._tag_commit(tag)
        if observed is None:
            return _absent(tag, tag_commit)
        if tag_commit is None and not observed.is_draft:
            msg = (
                f"published release {tag!r} does not resolve to a tag ref; "
                "refusing to accept a publication with no confirmed target"
            )
            raise ProverToolError(msg)
        return dc.replace(
            observed,
            target_commitish=tag_commit or observed.target_commitish,
            provenance_verified=False,
            provenance_reason=None,
        )

    def _tag_commit(self, tag: str) -> str | None:
        """Resolve `tag` to the commit it addresses, or `None` when absent."""
        current = _optional_object(
            f"repos/{self._repository}/git/ref/tags/{tag}", f"tag ref {tag}"
        )
        if current is None:
            return None
        peels = 0
        while True:
            kind, sha = current
            if kind == "commit":
                return sha
            if kind != "tag":
                msg = f"tag {tag!r} addresses a {kind!r}, not a commit"
                raise ProverToolError(msg)
            # Count only tag-object fetches. The terminal commit is fetched by
            # the last allowed peel and its type is checked at the loop top.
            if peels >= _TAG_PEEL_LIMIT:
                msg = (
                    f"tag {tag!r} chains through more than "
                    f"{_TAG_PEEL_LIMIT} annotated tag objects"
                )
                raise ProverToolError(msg)
            peels += 1
            current = _optional_object(
                f"repos/{self._repository}/git/tags/{sha}", f"tag object {sha}"
            )
            if current is None:
                msg = f"tag {tag!r} names a missing tag object {sha}"
                raise ProverToolError(msg)

    def _observe_by_tag(self, tag: str) -> ObservedRelease | None:
        """Read one release by tag, or `None` when the endpoint has no answer."""
        stdout = gh_optional((
            "gh",
            "api",
            f"repos/{self._repository}/releases/tags/{tag}",
            "--jq",
            _RELEASE_QUERY,
        ))
        if stdout is None:
            return None
        return parse_release(decoded(stdout))

    def _list_page(self, page: int) -> list[object]:
        """Read one page of the release list."""
        path = (
            f"repos/{self._repository}/releases"
            f"?per_page={_RELEASE_PAGE_SIZE}&page={page}"
        )
        items = decoded(
            gh((
                "gh",
                "api",
                path,
                "--jq",
                f"[.[] | {_RELEASE_QUERY}]",
            ))
        )
        if not isinstance(items, list):
            msg = "release API list response must be an array"
            raise ProverToolError(msg)
        return typ.cast("list[object]", items)

    def _observe_by_list(self, tag: str) -> ObservedRelease | None:
        """Search the bounded release list, which is where drafts are visible."""
        for page in range(1, _RELEASE_PAGE_LIMIT + 1):
            records = self._list_page(page)
            found = _find_tag(records, tag)
            if found is not None:
                return found
            if len(records) < _RELEASE_PAGE_SIZE:
                return None
        msg = (
            f"release list reached its {_RELEASE_PAGE_LIMIT}-page bound without "
            f"finding {tag}; raise the bound deliberately after reviewing older "
            "releases"
        )
        raise ProverToolError(msg)

    def verify(
        self,
        tag: str,
        observed: ObservedRelease,
        names: cabc.Sequence[str],
    ) -> VerifiedAssets:
        """Download the release's assets, hash them and authenticate its evidence.

        Every asset the observation reports is downloaded, not only the names
        the caller asked about, because the manifest, checksums and attestation
        can only authenticate each other as a complete set.
        """
        return verify_assets(self._repository, tag, observed, names)

    def create_draft(self, expected: ExpectedRelease) -> None:
        """Create a draft pre-release that records its run and build inputs."""
        gh(
            (
                "gh",
                "api",
                "--method",
                "POST",
                f"repos/{self._repository}/releases",
                "-f",
                f"tag_name={expected.tag}",
                "-f",
                f"target_commitish={expected.builder_commit}",
                "-f",
                f"name={expected.tag}",
                "-f",
                "body="
                + release_body(
                    expected.tag,
                    expected.input_fingerprint,
                    run_id=expected.run_id,
                    run_attempt=expected.run_attempt,
                ),
                "-F",
                "draft=true",
                "-F",
                "prerelease=true",
            ),
            timeout_seconds=TRANSFER_TIMEOUT_SECONDS,
        )

    def discard(self, tag: str, asset: ObservedAsset) -> None:
        """Delete one asset record that holds no uploaded bytes.

        GitHub refuses to upload a second asset under a name it already lists,
        so a record left behind by an interrupted upload must be removed before
        the real file can be sent. Only a record the policy classified as a
        placeholder is ever passed here; verified bytes are never deleted.
        """
        if not asset.asset_id:
            msg = f"asset {asset.name!r} has no server identifier to delete"
            raise ProverToolError(msg)
        gh(
            (
                "gh",
                "api",
                "--method",
                "DELETE",
                f"repos/{self._repository}/releases/assets/{asset.asset_id}",
            ),
            timeout_seconds=TRANSFER_TIMEOUT_SECONDS,
        )

    def upload(self, tag: str, asset: ReleaseAsset, path: Path) -> None:
        """Upload one asset without clobbering an existing one."""
        gh(
            (
                "gh",
                "release",
                "upload",
                tag,
                str(path),
                "--repo",
                self._repository,
            ),
            timeout_seconds=TRANSFER_TIMEOUT_SECONDS,
        )

    def publish(self, tag: str, release_id: int) -> None:
        """Make a verified draft public without marking it latest."""
        gh(
            (
                "gh",
                "api",
                "--method",
                "PATCH",
                f"repos/{self._repository}/releases/{release_id}",
                "-F",
                "draft=false",
                "-f",
                "make_latest=false",
            ),
            timeout_seconds=TRANSFER_TIMEOUT_SECONDS,
        )
