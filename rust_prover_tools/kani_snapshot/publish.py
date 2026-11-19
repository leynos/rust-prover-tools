"""Release publication orchestration over a tested GitHub adapter.

`release_policy` decides what a state means; this module performs the effect and
then reads the state again. Every step is therefore recoverable: a publication
call that times out may have succeeded, so the next observation decides what
actually happened instead of the caller assuming either outcome. The adapter
itself lives in `gh_release_api`; this module only depends on the `ReleaseApi`
port, so the orchestration is testable against any conforming double.
"""

from __future__ import annotations

import dataclasses as dc
import typing as typ

from rust_prover_tools.downloads import sha256_file
from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_snapshot.gh_release_api import GhReleaseApi
from rust_prover_tools.kani_snapshot.release_policy import (
    ExpectedRelease,
    ObservedRelease,
    ReleaseAction,
    ReleaseAsset,
    ReleaseDecision,
    ReleaseTransientError,
    ValidationBinding,
    decide,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

__all__ = (
    "GhReleaseApi",
    "PublicationRequest",
    "ReleaseApi",
    "publish_release",
)

#: A publication converges in a handful of observations. A larger bound would
#: only hide a state the policy cannot reconcile.
_MAX_STEPS: typ.Final[int] = 16


class ReleaseApi(typ.Protocol):
    """The release operations publication needs from an adapter."""

    def observe(self, tag: str) -> ObservedRelease:
        """Return the current release state, with downloaded asset digests."""
        ...

    def create_draft(self, expected: ExpectedRelease) -> None:
        """Create a draft release at the expected builder commit."""
        ...

    def upload(self, tag: str, asset: ReleaseAsset, path: Path) -> None:
        """Upload one missing asset without overwriting anything."""
        ...

    def verify_assets(self, tag: str, names: cabc.Sequence[str]) -> dict[str, str]:
        """Download and hash the named assets, returning their digests."""
        ...

    def publish(self, tag: str, release_id: int) -> None:
        """Make a complete, verified draft public."""
        ...


@dc.dataclass(frozen=True, slots=True)
class PublicationRequest:
    """Everything one publication attempt needs.

    Attributes
    ----------
    expected : ExpectedRelease
        Immutable identity to publish.
    validation : ValidationBinding
        Same-run clean-validation evidence.
    asset_paths : collections.abc.Mapping of str to Path
        Local file for each expected asset name.
    """

    expected: ExpectedRelease
    validation: ValidationBinding
    asset_paths: cabc.Mapping[str, Path]


def _require_local_asset(asset: ReleaseAsset, path: Path | None) -> None:
    """Require one local file to hold exactly the bytes the manifest records."""
    if path is None:
        msg = f"publication is missing local asset {asset.name!r}"
        raise ProverToolError(msg)
    try:
        size = path.stat().st_size
    except OSError as exc:
        msg = f"failed to stat release asset '{path}': {exc}"
        raise ProverToolError(msg) from exc
    if size != asset.size or sha256_file(path) != asset.sha256:
        msg = f"local release asset {asset.name!r} does not match its manifest"
        raise ProverToolError(msg)


def _check_local_assets(request: PublicationRequest) -> None:
    """Require every local upload to match the expected bytes before any call."""
    names = {asset.name for asset in request.expected.assets}
    extra = sorted(set(request.asset_paths) - names)
    if extra:
        msg = f"publication names local assets outside the expected set: {extra}"
        raise ProverToolError(msg)
    for asset in request.expected.assets:
        _require_local_asset(asset, request.asset_paths.get(asset.name))


def _upload(
    api: ReleaseApi,
    request: PublicationRequest,
    uploads: cabc.Sequence[ReleaseAsset],
) -> None:
    """Upload each missing asset from its recorded local path."""
    for asset in uploads:
        api.upload(request.expected.tag, asset, request.asset_paths[asset.name])


def _publish(
    api: ReleaseApi,
    request: PublicationRequest,
    observed: ObservedRelease,
) -> None:
    """Make the observed draft public, refusing one without an identifier."""
    if observed.release_id is None:
        msg = "cannot publish a draft without its release identifier"
        raise ProverToolError(msg)
    api.publish(request.expected.tag, observed.release_id)


def _apply(
    api: ReleaseApi,
    request: PublicationRequest,
    decision: ReleaseDecision,
    observed: ObservedRelease,
) -> dict[str, str] | None:
    """Perform one effectful step.

    Returns the digests a verification produced, or `None` when the step
    changed server state and the caller must observe again.
    """
    if decision.action is ReleaseAction.CREATE_DRAFT:
        api.create_draft(request.expected)
        return None
    if decision.action is ReleaseAction.UPLOAD_ASSETS:
        _upload(api, request, decision.uploads)
        return None
    if decision.action is ReleaseAction.VERIFY_ASSETS:
        return api.verify_assets(request.expected.tag, decision.verifications)
    if decision.action is ReleaseAction.PUBLISH:
        _publish(api, request, observed)
        return None
    return None


def _with_digests(
    observed: ObservedRelease,
    digests: cabc.Mapping[str, str],
) -> ObservedRelease:
    """Return an observation whose assets carry freshly verified digests."""
    return dc.replace(
        observed,
        assets=tuple(
            dc.replace(asset, sha256=digests.get(asset.name, asset.sha256))
            for asset in observed.assets
        ),
    )


def _observe_or_keep(
    api: ReleaseApi,
    tag: str,
    observed: ObservedRelease | None,
) -> ObservedRelease | None:
    """Observe the release once, keeping the previous state after a retryable error."""
    if observed is not None:
        return observed
    try:
        return api.observe(tag)
    except ReleaseTransientError:
        return None


def publish_release(
    request: PublicationRequest,
    api: ReleaseApi,
    *,
    max_steps: int = _MAX_STEPS,
) -> ReleaseDecision:
    """Drive an immutable release from its current state to publication.

    Parameters
    ----------
    request : PublicationRequest
        Expected identity, validation evidence and local asset paths.
    api : ReleaseApi
        Adapter that reads and writes release state.
    max_steps : int
        Maximum observed transitions before the attempt is refused.

    Returns
    -------
    ReleaseDecision
        The final `NOOP` decision for a published, matching release.

    Raises
    ------
    ProverToolError
        Raised for a local asset mismatch, an unreconcilable conflict, or a
        state that does not converge within `max_steps`.
    """
    _check_local_assets(request)
    observed: ObservedRelease | None = None
    for _ in range(max_steps):
        observed = _observe_or_keep(api, request.expected.tag, observed)
        if observed is None:
            continue
        decision = decide(request.expected, observed, request.validation)
        if decision.action is ReleaseAction.NOOP:
            return decision
        if decision.action is ReleaseAction.CONFLICT:
            msg = f"publication conflict: {decision.reason}"
            raise ProverToolError(msg)
        try:
            digests = _apply(api, request, decision, observed)
        except ReleaseTransientError:
            # The effect may have landed before the failure was reported, so
            # the next loop observes state instead of repeating the write.
            observed = None
            continue
        observed = _with_digests(observed, digests) if digests is not None else None
    msg = f"publication did not converge after {max_steps} observed steps"
    raise ProverToolError(msg)
