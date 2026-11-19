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
from rust_prover_tools.kani_snapshot.models import REPOSITORY
from rust_prover_tools.kani_snapshot.release_model import (
    ExpectedRelease,
    ObservedRelease,
    ReleaseAsset,
    ReleaseTransientError,
    release_body,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

__all__ = ("GhReleaseApi",)

#: Cuprum terminates a command that exceeds its timeout and reports this code.
_TIMEOUT_EXIT_CODE: typ.Final[int] = 124

#: Transport-shaped markers that make a failed call worth re-observing.
_TRANSIENT_MARKERS: typ.Final[tuple[str, ...]] = (
    "HTTP 502",
    "HTTP 503",
    "HTTP 504",
    "HTTP 500",
    "timeout",
)

#: Releases requested per page when the tag endpoint cannot see a draft.
_RELEASE_PAGE_SIZE: typ.Final[int] = 100

#: Pages the draft lookup may read. Reaching it is a failure, not an absence.
_RELEASE_PAGE_LIMIT: typ.Final[int] = 10

#: Fields every observation needs, from either release endpoint.
_RELEASE_QUERY: typ.Final[str] = (
    "{id, tag_name, draft, target_commitish, body, assets: "
    "[.assets[] | {name, size, digest}]}"
)


def _gh(argv: cabc.Sequence[str]) -> str:
    """Run one `gh` command, classifying transport failures.

    Parameters
    ----------
    argv : collections.abc.Sequence of str
        Complete argument vector.

    Returns
    -------
    str
        Captured standard output.

    Raises
    ------
    ReleaseTransientError
        Raised for a timeout, a connection failure or a retryable server error,
        because a fresh observation may still reconcile the attempt.
    ProverToolError
        Raised for a permanent failure such as an authentication refusal.
    """
    result = run_command(CommandSpec(tuple(argv)))
    if result.exit_code == 0:
        return result.stdout
    stderr = result.stderr.strip()
    if _is_transient(result.exit_code, stderr):
        msg = f"transient release API failure: {stderr or result.exit_code}"
        raise ReleaseTransientError(msg)
    msg = f"release API command failed ({result.exit_code}): {stderr}"
    raise ProverToolError(msg)


def _is_transient(exit_code: int, stderr: str) -> bool:
    """Return whether a failed `gh` call is worth re-observing."""
    if exit_code == _TIMEOUT_EXIT_CODE:
        return True
    return any(marker in stderr for marker in _TRANSIENT_MARKERS)


def _is_not_found(stderr: str) -> bool:
    """Return whether a failed `gh api` call means the release does not exist."""
    return "HTTP 404" in stderr or "not found" in stderr.lower()


def _find_tag(records: list[object], tag: str) -> ObservedRelease | None:
    """Return the listed release whose tag matches, or `None` when absent."""
    for item in records:
        candidate = parse_release(item)
        if candidate.tag == tag:
            return candidate
    return None


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

        A published release's own manifest, checksums and payload digests are
        re-verified from the downloaded bytes, so a later run need not compare
        its new asset bytes against the published ones.
        """
        observed = self._observe_by_tag(tag)
        if observed is None:
            observed = self._observe_by_list(tag)
        if observed is None:
            return ObservedRelease(exists=False, is_draft=False)
        return self._with_verified_assets(tag, observed)

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
        if _is_not_found(result.stderr):
            return None
        if _is_transient(result.exit_code, result.stderr):
            msg = f"transient release API failure: {result.stderr.strip()}"
            raise ReleaseTransientError(msg)
        msg = f"release API command failed ({result.exit_code}): {result.stderr}"
        raise ProverToolError(msg)

    def _list_page(self, page: int) -> list[object]:
        """Read one page of the release list."""
        path = (
            f"repos/{self._repository}/releases"
            f"?per_page={_RELEASE_PAGE_SIZE}&page={page}"
        )
        items = decoded(
            _gh((
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
            verified = not observed.is_draft and verify_publication(directory, names)
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
            _gh((
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
        _gh((
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
        _gh((
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
        _gh((
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
