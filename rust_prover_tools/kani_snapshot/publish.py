"""Release publication orchestration over a tested GitHub adapter.

`release_policy` decides what a state means, `publish_steps` performs the effect
that decision names, and this module reads the state again after every step.
Every step is therefore recoverable: a publication call that times out may have
succeeded, so the next observation decides what actually happened instead of
the caller assuming either outcome. Publication is the one write that cannot be
taken back, so it is preceded by a fresh observation and a second decision:
state that stopped authorising the write is acted on instead of being published
from a stale reading. The adapter itself lives in `gh_release.api`; this module
only depends on the `ReleaseApi` port, so the orchestration is testable against
any conforming double.

What a spent budget may claim is decided in `publish_budget`; this module owns
the loop, and hands that module the facts it needs to describe the ending.
"""

from __future__ import annotations

import logging
import typing as typ

from rust_prover_tools.downloads import sha256_file
from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_snapshot.publish_budget import (
    PublicationUncertainError,
    decide_exhausted,
    uncertain_after_write,
)
from rust_prover_tools.kani_snapshot.publish_observation import observe_or_keep
from rust_prover_tools.kani_snapshot.publish_request import PublicationRequest
from rust_prover_tools.kani_snapshot.publish_steps import advance
from rust_prover_tools.kani_snapshot.release_policy import (
    ObservedRelease,
    ReleaseAsset,
    ReleaseDecision,
)
from rust_prover_tools.kani_snapshot.release_port import ReleaseApi
from rust_prover_tools.observability import OperationSpan, log_event, operation_span

if typ.TYPE_CHECKING:
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


def _run_steps(
    request: PublicationRequest,
    api: ReleaseApi,
    max_steps: int,
) -> tuple[ObservedRelease | None, bool, ReleaseDecision | None]:
    """Apply policy step by step until the state converges or the budget ends.

    Returns the newest reading the run holds, whether any step entered the
    publish call, and the `NOOP` decision when the state converged.
    """
    observed: ObservedRelease | None = None
    publish_attempted = False
    for step in range(1, max_steps + 1):
        try:
            observed = observe_or_keep(api, request.expected.tag, observed)
            if observed is None:
                log_event(_LOGGER, "kani.snapshot.publish.deferred", step=step)
                continue
            advanced = advance(api, request, observed, step)
        except ProverToolError as exc:
            # A later step's failure cannot be reported as though the write
            # never happened: the release may already be public. A failure
            # already wrapped where it happened keeps its one warning.
            if isinstance(exc, PublicationUncertainError) or not publish_attempted:
                raise
            raise uncertain_after_write(exc) from exc
        if isinstance(advanced, ReleaseDecision):
            return observed, publish_attempted, advanced
        publish_attempted = publish_attempted or advanced.write_attempted
        observed = advanced.observed
    return observed, publish_attempted, None


def _converge(
    request: PublicationRequest,
    api: ReleaseApi,
    max_steps: int,
    span: OperationSpan,
) -> ReleaseDecision:
    """Apply policy to successive observations until publication converges."""
    observed, publish_attempted, converged = _run_steps(request, api, max_steps)
    if converged is not None:
        span.finish("noop")
        return converged
    if observed is None and publish_attempted:
        # The write may have landed and its response was never read, so the
        # state is read once more before the ending is described. A read that
        # fails permanently leaves that uncertainty to report, not a bare error.
        try:
            observed = observe_or_keep(api, request.expected.tag, None)
        except ProverToolError as exc:
            raise uncertain_after_write(exc) from exc
    decision = decide_exhausted(
        request, observed, span, publish_attempted=publish_attempted
    )
    if decision is not None:
        return decision
    span.finish("not_converged")
    msg = f"publication did not converge after {max_steps} observed steps"
    raise ProverToolError(msg)


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


def require_local_assets(request: PublicationRequest) -> None:
    """Require every local upload to match the expected bytes before any call.

    The check belongs here rather than in the request record: it reads and
    hashes files, which the orchestration may do, and which the record that
    describes one attempt must not.
    """
    names = {asset.name for asset in request.expected.assets}
    extra = sorted(set(request.asset_paths) - names)
    if extra:
        msg = f"publication names local assets outside the expected set: {extra}"
        raise ProverToolError(msg)
    for asset in request.expected.assets:
        _require_local_asset(asset, request.asset_paths.get(asset.name))


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
        Raised for a local asset mismatch, an unreconcilable conflict, a state
        that does not converge within `max_steps`, or a budget that ended on a
        publish that could not be confirmed.
    """
    with operation_span(_LOGGER, "kani.snapshot.publish") as span:
        require_local_assets(request)
        return _converge(request, api, max_steps, span)
