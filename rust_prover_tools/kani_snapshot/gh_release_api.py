"""GitHub release adapter for the publication state machine.

`publish` owns the orchestration and `release_policy` owns the decisions; this
module only turns those decisions into `gh` argument vectors and reads the
resulting state back. The adapter is deliberately narrow: `GhReleaseApi` builds
argument vectors through `rust_prover_tools.commands`, classifies transport
failures as transient, and re-downloads every expected asset to hash it. That
last rule is what makes `ObservedAsset.sha256` evidence rather than a claim: the
digest that authorises a publish is computed locally from the bytes the public
release serves.
"""

from __future__ import annotations

import dataclasses as dc
import pathlib
import tempfile
import typing as typ

from rust_prover_tools.commands import CommandSpec, run_command
from rust_prover_tools.downloads import sha256_file
from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_snapshot.gh_release_document import (
    decoded,
    parse_release,
    verify_publication,
)
from rust_prover_tools.kani_snapshot.gh_transport import failed, gh, is_not_found
from rust_prover_tools.kani_snapshot.models import REPOSITORY
from rust_prover_tools.kani_snapshot.release_model import (
    ExpectedRelease,
    ObservedRelease,
    ReleaseAsset,
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

#: Releases requested per page when the tag endpoint cannot see a draft.
_RELEASE_PAGE_SIZE: typ.Final[int] = 100

#: Pages the draft lookup may read. Reaching it is a failure, not an absence.
_RELEASE_PAGE_LIMIT: typ.Final[int] = 10

#: How many annotated tag objects one tag may chain through before it is refused.
_TAG_PEEL_LIMIT: typ.Final[int] = 4

#: Fields every observation needs, from either release endpoint.
_RELEASE_QUERY: typ.Final[str] = (
    "{id, tag_name, draft, target_commitish, body, assets: "
    "[.assets[] | {name, size, digest}]}"
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
    result = run_command(CommandSpec(("gh", "api", path, "--jq", ".object")))
    if result.exit_code == 0:
        return _object_document(decoded(result.stdout), field)
    if is_not_found(result.stderr):
        return None
    raise failed(result.exit_code, result.stderr)


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
        """Return the release state, re-downloading every asset's bytes.

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

        A published release's own manifest, checksums, payloads, attestation
        and identity are re-verified from the downloaded bytes, so a later run
        need not compare its new asset bytes against the published ones.
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
        return self._with_verified_assets(
            tag,
            dc.replace(
                observed,
                target_commitish=tag_commit or observed.target_commitish,
            ),
        )

    def _tag_commit(self, tag: str) -> str | None:
        """Resolve `tag` to the commit it addresses, or `None` when absent."""
        current = _optional_object(
            f"repos/{self._repository}/git/ref/tags/{tag}", f"tag ref {tag}"
        )
        if current is None:
            return None
        for _ in range(_TAG_PEEL_LIMIT):
            kind, sha = current
            if kind == "commit":
                return sha
            if kind != "tag":
                msg = f"tag {tag!r} addresses a {kind!r}, not a commit"
                raise ProverToolError(msg)
            current = _optional_object(
                f"repos/{self._repository}/git/tags/{sha}", f"tag object {sha}"
            )
            if current is None:
                msg = f"tag {tag!r} names a missing tag object {sha}"
                raise ProverToolError(msg)
        msg = f"tag {tag!r} chains through more than {_TAG_PEEL_LIMIT} objects"
        raise ProverToolError(msg)

    def _observe_by_tag(self, tag: str) -> ObservedRelease | None:
        """Read one release by tag, or `None` when the endpoint has no answer."""
        result = run_command(
            CommandSpec((
                "gh",
                "api",
                f"repos/{self._repository}/releases/tags/{tag}",
                "--jq",
                _RELEASE_QUERY,
            ))
        )
        if result.exit_code == 0:
            return parse_release(decoded(result.stdout))
        if is_not_found(result.stderr):
            return None
        raise failed(result.exit_code, result.stderr)

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

    def _with_verified_assets(
        self,
        tag: str,
        observed: ObservedRelease,
    ) -> ObservedRelease:
        """Download every asset, recording digests and publication provenance."""
        names = [asset.name for asset in observed.assets]
        with tempfile.TemporaryDirectory(prefix="kani-publish-") as name:
            directory = pathlib.Path(name)
            self._download(tag, names, directory)
            assets = tuple(
                dc.replace(asset, sha256=sha256_file(directory / asset.name))
                for asset in observed.assets
            )
            verified = not observed.is_draft and verify_publication(
                directory,
                names,
                tag=tag,
                body=observed.body,
            )
        return dc.replace(
            observed,
            assets=assets,
            provenance_verified=verified,
        )

    def _download(
        self,
        tag: str,
        names: cabc.Sequence[str],
        directory: Path,
    ) -> None:
        """Download the named assets into one directory."""
        for name in names:
            gh((
                "gh",
                "release",
                "download",
                tag,
                "--repo",
                self._repository,
                "--pattern",
                name,
                "--dir",
                str(directory),
            ))

    def create_draft(self, expected: ExpectedRelease) -> None:
        """Create a draft pre-release that records its run and build inputs."""
        gh((
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
        ))

    def upload(self, tag: str, asset: ReleaseAsset, path: Path) -> None:
        """Upload one asset without clobbering an existing one."""
        gh((
            "gh",
            "release",
            "upload",
            tag,
            str(path),
            "--repo",
            self._repository,
        ))

    def verify_assets(self, tag: str, names: cabc.Sequence[str]) -> dict[str, str]:
        """Download the named assets and return their locally computed digests."""
        if not names:
            return {}
        with tempfile.TemporaryDirectory(prefix="kani-publish-") as name:
            directory = pathlib.Path(name)
            self._download(tag, names, directory)
            return {asset: sha256_file(directory / asset) for asset in names}

    def publish(self, tag: str, release_id: int) -> None:
        """Make a verified draft public without marking it latest."""
        gh((
            "gh",
            "api",
            "--method",
            "PATCH",
            f"repos/{self._repository}/releases/{release_id}",
            "-F",
            "draft=false",
            "-f",
            "make_latest=false",
        ))
