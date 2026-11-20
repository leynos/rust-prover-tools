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

from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_snapshot.publish_request import (
    PublicationRequest,
    require_local_assets,
)
from rust_prover_tools.kani_snapshot.release_policy import (
    ObservedAsset,
    ObservedRelease,
    ReleaseAction,
    ReleaseAsset,
    ReleaseDecision,
    ReleaseTransientError,
    VerifiedAssets,
    decide,
)
from rust_prover_tools.kani_snapshot.release_port import ReleaseApi
from rust_prover_tools.observability import OperationSpan, log_event, operation_span

if typ.TYPE_CHECKING:
    import collections.abc as cabc

__all__ = (
    "PublicationRequest",
    "ReleaseApi",
    "publish_release",
)

_LOGGER = logging.getLogger(__name__)

#: A publication converges in a handful of observations. A larger bound would
#: only hide a state the policy cannot reconcile.
_MAX_STEPS: typ.Final[int] = 16


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


def _state_key(observed: ObservedRelease) -> tuple[object, ...]:
    """Return the parts of an observation the policy decides from.

    Every field the policy reads is included, not only the asset set: a draft
    can be retargeted or re-bodied while its bytes are being downloaded, and
    the newer reading is the one that would be refused. Only the computed
    digests are left out, because the two readings sit on either side of a
    download, so one of them carries hashes the other cannot, and that
    difference alone is not a change of state.
    """
    return (
        observed.exists,
        observed.is_draft,
        observed.release_id,
        observed.tag,
        observed.target_commitish,
        observed.body,
        tuple(
            sorted(
                (asset.name, asset.size, asset.asset_id) for asset in observed.assets
            )
        ),
    )


def _settle(
    api: ReleaseApi,
    tag: str,
    observed: ObservedRelease,
    completed: ObservedRelease,
) -> ObservedRelease:
    """Return the reading a completed download may be decided from.

    A download takes time, and its listing may have gone out of date by the
    time it finishes: an asset can be removed after its own bytes were read, a
    draft retargeted, a release's notes rewritten. The completed reading only
    describes the release the policy saw if the release still matches it, so
    the state is read once more and the completed reading is discarded when it
    does not. The newer reading then carries no verification verdict, and the
    next decision asks for the download it needs rather than trusting one taken
    across a change.
    """
    confirmed = api.observe(tag)
    if _state_key(confirmed) != _state_key(observed):
        return confirmed
    return completed


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

    The download takes time, and the listing it was started from is not
    evidence of what is still there when it finishes: an asset removed after
    its own bytes were hashed would otherwise be published missing, and a draft
    retargeted or re-bodied mid-download would be published from an identity
    the policy never approved. So the state is read once more after the
    verification, and the decision is made from that reading. When the two
    readings agree, the verified observation is the one decided on, because it
    carries the digests and the provenance verdict the second reading cannot;
    when they disagree, the draft moved while it was being read and the
    verified set no longer describes it, so the newer reading is decided on
    instead.

    The recheck narrows the window before the write; it does not close it.
    GitHub's release update has no conditional form, so the state can still
    change between this decision and the PATCH. What the recheck guarantees is
    that publication is never started from a stale reading, not that the write
    is atomic. The observation after the write is what reports any divergence
    that slipped into that window, and it is the reason a publish call is
    always followed by another observation rather than assumed to have landed
    in the state that authorised it.
    """
    observed = api.observe(request.expected.tag)
    names = tuple(asset.name for asset in observed.assets)
    if not names:
        return observed, decide(request.expected, observed, request.validation)
    verified = _with_verification(
        observed,
        api.verify(request.expected.tag, observed, names),
    )
    settled = _settle(api, request.expected.tag, observed, verified)
    return settled, decide(request.expected, settled, request.validation)


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
    """Perform one step other than publication.

    Returns the observation a hash or verification completed, or `None` when
    the step changed server state and the caller must observe again.
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
    if decision.action is ReleaseAction.HASH_ASSETS:
        # A byte check before an edit, not a provenance verdict: only the
        # named digests are learned, and the draft is not yet authenticated.
        digests = api.hash_assets(
            request.expected.tag, observed, decision.verifications
        )
        hashed = dc.replace(observed, assets=_with_digests(observed.assets, digests))
        # The hashes describe the bytes named by this listing. An edit made
        # from them would be authorised by a state that may have moved while
        # they were being read, so the state is confirmed first.
        return _settle(api, request.expected.tag, observed, hashed)
    if decision.action is ReleaseAction.VERIFY_ASSETS:
        verified = api.verify(request.expected.tag, observed, decision.verifications)
        settled = _with_verification(observed, verified)
        # A published release's notes stay editable, and its assets can be
        # replaced by same-named ones; a verdict reached against the old
        # reading must not outlive it.
        return _settle(api, request.expected.tag, observed, settled)
    return None


def _with_digests(
    assets: cabc.Iterable[ObservedAsset],
    digests: cabc.Iterable[tuple[str, str]],
) -> tuple[ObservedAsset, ...]:
    """Return the assets with the digests computed for their bytes.

    An asset whose bytes were not hashed keeps whatever digest it already
    reported, so a partial hash narrows the unknown set and never invents
    evidence for a file nobody downloaded.
    """
    computed = dict(digests)
    return tuple(
        dc.replace(asset, sha256=computed.get(asset.name, asset.sha256))
        for asset in assets
    )


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
        require_local_assets(request)
        return _converge(request, api, max_steps, span)
