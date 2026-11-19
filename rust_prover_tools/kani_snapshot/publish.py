"""Release publication orchestration over a tested GitHub adapter.

`release_policy` decides what a state means; this module performs the effect and
then reads the state again. Every step is therefore recoverable: a publication
call that times out may have succeeded, so the next observation decides what
actually happened instead of the caller assuming either outcome. Publication is
the one write that cannot be taken back, so it is preceded by a fresh
observation and a second decision: state that stopped authorising the write is
acted on instead of being published from a stale reading. The adapter
itself lives in `gh_release.api`; this module only depends on the `ReleaseApi`
port, so the orchestration is testable against any conforming double.
"""

from __future__ import annotations

import dataclasses as dc
import logging
import typing as typ

from rust_prover_tools.downloads import sha256_file
from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_snapshot.release_policy import (
    ExpectedRelease,
    ObservedAsset,
    ObservedRelease,
    ReleaseAction,
    ReleaseAsset,
    ReleaseDecision,
    ReleaseTransientError,
    ValidationBinding,
    VerifiedAssets,
    decide,
)
from rust_prover_tools.kani_snapshot.release_port import ReleaseApi
from rust_prover_tools.observability import OperationSpan, log_event, operation_span

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

__all__ = (
    "PublicationRequest",
    "ReleaseApi",
    "publish_release",
)

_LOGGER = logging.getLogger(__name__)

#: A publication converges in a handful of observations. A larger bound would
#: only hide a state the policy cannot reconcile.
_MAX_STEPS: typ.Final[int] = 16


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


def _discard(
    api: ReleaseApi,
    request: PublicationRequest,
    placeholders: cabc.Sequence[ObservedAsset],
) -> None:
    """Delete the records an interrupted upload left behind."""
    for asset in placeholders:
        api.discard(request.expected.tag, asset)


def _recheck(
    api: ReleaseApi,
    request: PublicationRequest,
) -> tuple[ObservedRelease, ReleaseDecision]:
    """Observe the draft again and decide from the bytes it serves now.

    The fresh observation is completed by verifying every asset it reports,
    even when the API already volunteers a digest. A digest in a listing is a
    claim, and the evidence that authorises publication is the authenticated
    download: only a verification run against the current bytes can say the
    draft still deserves to become public.

    The recheck narrows the window before the write; it does not close it.
    GitHub's release update has no conditional form, so the state can still
    change between this decision and the PATCH. What the recheck guarantees is
    that publication is never started from a stale reading, not that the write
    is atomic. The observation after the write is what reports any divergence
    that slipped into that window, and it is the reason a publish call is
    always followed by another observation rather than assumed to have landed
    in the state that authorised it.
    """
    fresh = api.observe(request.expected.tag)
    names = tuple(asset.name for asset in fresh.assets)
    if names:
        fresh = _with_verification(
            fresh,
            api.verify(request.expected.tag, fresh, names),
        )
    return fresh, decide(request.expected, fresh, request.validation)


def _publish(
    api: ReleaseApi,
    request: PublicationRequest,
    observed: ObservedRelease,
) -> ObservedRelease | None:
    """Make a draft public only while a fresh observation still authorises it.

    The observation that authorised publication downloaded the draft's bytes,
    but its asset list can change before the write. A maintainer who removes an
    asset in that window would otherwise be published a release whose bytes
    nobody verified. Re-observing binds the write to state read immediately
    before it: when the state no longer authorises publication, the fresh
    observation is returned so the next pass applies the policy to it instead
    of writing. `None` asks the caller to observe again after the write, whose
    outcome cannot be assumed from a successful call.
    """
    if observed.release_id is None:
        msg = "cannot publish a draft without its release identifier"
        raise ProverToolError(msg)
    fresh, recheck = _recheck(api, request)
    if recheck.action is not ReleaseAction.PUBLISH:
        return fresh
    if fresh.release_id is None:
        msg = "cannot publish a draft without its release identifier"
        raise ProverToolError(msg)
    api.publish(request.expected.tag, fresh.release_id)
    return None


def _apply(
    api: ReleaseApi,
    request: PublicationRequest,
    decision: ReleaseDecision,
    observed: ObservedRelease,
) -> ObservedRelease | None:
    """Perform one effectful step other than publication.

    Returns the observation a verification completed, or `None` when the step
    changed server state and the caller must observe again.
    """
    if decision.action is ReleaseAction.CREATE_DRAFT:
        api.create_draft(request.expected)
        return None
    if decision.action is ReleaseAction.DISCARD_PLACEHOLDER:
        _discard(api, request, decision.discards)
        return None
    if decision.action is ReleaseAction.UPLOAD_ASSETS:
        _upload(api, request, decision.uploads)
        return None
    if decision.action is ReleaseAction.VERIFY_ASSETS:
        verified = api.verify(request.expected.tag, observed, decision.verifications)
        return _with_verification(observed, verified)
    return None


def _with_verification(
    observed: ObservedRelease,
    verified: VerifiedAssets,
) -> ObservedRelease:
    """Return an observation completed by the bytes it was verified against."""
    digests = verified.digest_map()
    return dc.replace(
        observed,
        assets=tuple(
            dc.replace(asset, sha256=digests.get(asset.name, asset.sha256))
            for asset in observed.assets
        ),
        provenance_verified=verified.provenance_verified,
        provenance_reason=verified.reason,
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


def _step(
    api: ReleaseApi,
    request: PublicationRequest,
    decision: ReleaseDecision,
    observed: ObservedRelease,
) -> ObservedRelease | None:
    """Perform one decision's effect, re-observing after a transient failure.

    The write may have landed before its failure was reported, so the next
    observation decides what actually happened rather than the caller repeating
    a write that already took effect. The failing action is enough to identify
    the step: the decision event logged immediately before this call records
    the same action beside its step number.
    """
    try:
        if decision.action is ReleaseAction.PUBLISH:
            return _publish(api, request, observed)
        return _apply(api, request, decision, observed)
    except ReleaseTransientError:
        log_event(
            _LOGGER,
            "kani.snapshot.publish.transient",
            action=decision.action.value,
        )
        return None


def _converge(
    request: PublicationRequest,
    api: ReleaseApi,
    max_steps: int,
    span: OperationSpan,
) -> ReleaseDecision:
    """Apply policy to successive observations until publication converges."""
    observed: ObservedRelease | None = None
    for step in range(1, max_steps + 1):
        observed = _observe_or_keep(api, request.expected.tag, observed)
        if observed is None:
            log_event(_LOGGER, "kani.snapshot.publish.deferred", step=step)
            continue
        decision = decide(request.expected, observed, request.validation)
        log_event(
            _LOGGER,
            "kani.snapshot.publish.decision",
            step=step,
            action=decision.action.value,
        )
        if decision.action is ReleaseAction.NOOP:
            span.finish("noop")
            return decision
        if decision.action is ReleaseAction.CONFLICT:
            span.finish("conflict")
            msg = f"publication conflict: {decision.reason}"
            raise ProverToolError(msg)
        observed = _step(api, request, decision, observed)
    span.finish("not_converged")
    msg = f"publication did not converge after {max_steps} observed steps"
    raise ProverToolError(msg)


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
    with operation_span(_LOGGER, "kani.snapshot.publish") as span:
        _check_local_assets(request)
        return _converge(request, api, max_steps, span)
