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
import json
import pathlib
import tempfile
import typing as typ

from rust_prover_tools.commands import CommandSpec, run_command
from rust_prover_tools.downloads import sha256_file
from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_snapshot.models import REPOSITORY
from rust_prover_tools.kani_snapshot.release_model import (
    ExpectedRelease,
    ObservedAsset,
    ObservedRelease,
    ReleaseAsset,
    ReleaseTransientError,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

__all__ = ("GhReleaseApi",)

#: Cuprum terminates a command that exceeds its timeout and reports this code.
_TIMEOUT_EXIT_CODE: typ.Final[int] = 124

#: Width of the hexadecimal part of a SHA-256 digest.
_SHA256_HEX_LENGTH: typ.Final[int] = 64

#: Transport-shaped markers that make a failed call worth re-observing.
_TRANSIENT_MARKERS: typ.Final[tuple[str, ...]] = (
    "HTTP 502",
    "HTTP 503",
    "HTTP 504",
    "HTTP 500",
    "timeout",
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


def _is_sha256_hex(candidate: str) -> bool:
    """Return whether `candidate` is lowercase hexadecimal of the right width."""
    return len(candidate) == _SHA256_HEX_LENGTH and all(
        char in "0123456789abcdef" for char in candidate
    )


def _require_asset_size(name: str, size: object) -> int:
    """Return a validated asset size, refusing booleans and negatives."""
    msg = f"release asset {name!r} size must be a non-negative integer"
    if not isinstance(size, int) or isinstance(size, bool):
        raise ProverToolError(msg)
    if size < 0:
        raise ProverToolError(msg)
    return size


def _parse_asset(value: object) -> ObservedAsset:
    """Parse one asset record, refusing a malformed size or digest."""
    if not isinstance(value, dict):
        msg = "release asset API response must be an object"
        raise ProverToolError(msg)
    record = typ.cast("dict[str, object]", value)
    name = record.get("name")
    if not isinstance(name, str) or not name:
        msg = "release asset name must be a non-empty string"
        raise ProverToolError(msg)
    size = _require_asset_size(name, record.get("size"))
    digest = record.get("digest")
    sha256: str | None = None
    if isinstance(digest, str) and digest.startswith("sha256:"):
        candidate = digest.removeprefix("sha256:")
        if _is_sha256_hex(candidate):
            sha256 = candidate
    return ObservedAsset(name=name, size=size, sha256=sha256)


def _parse_release(document: object) -> ObservedRelease:
    """Parse one release API document into an observation without digests."""
    if not isinstance(document, dict):
        msg = "release API response must be an object"
        raise ProverToolError(msg)
    record = typ.cast("dict[str, object]", document)
    release_id = record.get("id")
    draft = record.get("draft")
    tag = record.get("tag_name")
    target = record.get("target_commitish")
    assets = record.get("assets")
    if not isinstance(release_id, int) or isinstance(release_id, bool):
        msg = "release API response id must be an integer"
        raise ProverToolError(msg)
    if not isinstance(draft, bool):
        msg = "release API response draft must be a boolean"
        raise ProverToolError(msg)
    if not isinstance(tag, str) or not isinstance(target, str):
        msg = "release API response tag and target must be strings"
        raise ProverToolError(msg)
    if not isinstance(assets, list):
        msg = "release API response assets must be an array"
        raise ProverToolError(msg)
    parsed = tuple(_parse_asset(asset) for asset in typ.cast("list[object]", assets))
    return ObservedRelease(
        exists=True,
        is_draft=draft,
        tag=tag,
        target_commitish=target,
        assets=parsed,
        release_id=release_id,
    )


def _decoded(result_stdout: str) -> object:
    """Decode a JSON response, refusing anything that is not JSON."""
    try:
        return json.loads(result_stdout)
    except json.JSONDecodeError as exc:
        msg = f"release API response is not JSON: {exc}"
        raise ProverToolError(msg) from exc


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

        A missing release is an observation, not an error. Any other permanent
        failure is refused so a publication cannot proceed blind.
        """
        query = (
            "{id, tag_name, draft, target_commitish, assets: "
            "[.assets[] | {name, size, digest}]}"
        )
        result = run_command(
            CommandSpec((
                "gh",
                "api",
                f"repos/{self._repository}/releases/tags/{tag}",
                "--jq",
                query,
            ))
        )
        if result.exit_code != 0:
            if _is_not_found(result.stderr):
                return ObservedRelease(exists=False, is_draft=False)
            if _is_transient(result.exit_code, result.stderr):
                msg = f"transient release API failure: {result.stderr.strip()}"
                raise ReleaseTransientError(msg)
            msg = f"release API command failed ({result.exit_code}): {result.stderr}"
            raise ProverToolError(msg)
        observed = _parse_release(_decoded(result.stdout))
        digests = self.verify_assets(tag, [asset.name for asset in observed.assets])
        return dc.replace(
            observed,
            assets=tuple(
                dc.replace(asset, sha256=digests.get(asset.name, asset.sha256))
                for asset in observed.assets
            ),
        )

    def create_draft(self, expected: ExpectedRelease) -> None:
        """Create a draft pre-release at the expected builder commit."""
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
            f"body=Immutable Kani snapshot {expected.tag}",
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
        digests: dict[str, str] = {}
        if not names:
            return digests
        with tempfile.TemporaryDirectory(prefix="kani-publish-") as directory:
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
                    directory,
                ))
                digests[name] = sha256_file(pathlib.Path(directory) / name)
        return digests

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
