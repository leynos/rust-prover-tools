"""What a publication's step budget leaves behind when it runs out.

The loop in `publish` applies the policy to one observed state per step and
stops when the state converges, when it conflicts, or when the budget is spent.
The last of those needs its own decision, because only one step is
irreversible: a run that stopped on the publish call may already have published
a release, a run whose recheck withdrew authority stopped short of the write,
and a run that stopped anywhere else has nothing outstanding. Reporting all
three the same way would tell an operator either to repeat work that cannot be
repeated or to accept a state the run never reached.

`StepOutcome` is how the loop says which of those happened: the reading a step
produced without writing, and whether the irreversible call was entered.
"""

from __future__ import annotations

import dataclasses as dc
import typing as typ

from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_snapshot.release_policy import (
    ObservedRelease,
    ReleaseAction,
    ReleaseDecision,
    decide,
)

if typ.TYPE_CHECKING:
    from rust_prover_tools.kani_snapshot.publish_request import PublicationRequest
    from rust_prover_tools.observability import OperationSpan

__all__ = ("StepOutcome", "decide_exhausted")


@dc.dataclass(frozen=True)
class StepOutcome:
    """What one executed step leaves for the next pass to establish.

    Attributes
    ----------
    observed : ObservedRelease | None
        A reading the step produced without writing. `None` asks the caller to
        observe again before deciding.
    write_attempted : bool
        Whether the irreversible call was entered. A call that was entered may
        have landed even when it reported a failure, so a budget that ends here
        cannot say that nothing happened.
    """

    observed: ObservedRelease | None = None
    write_attempted: bool = False


def decide_exhausted(
    request: PublicationRequest,
    observed: ObservedRelease | None,
    span: OperationSpan,
    *,
    publish_attempted: bool,
) -> ReleaseDecision | None:
    """Return the decision a spent budget supports, or raise what it cannot.

    The reading already in hand is decided on first, because a step can have
    produced one without writing: the recheck that withdrew authority for a
    publish returns the state that withdrew it, and that state deserves the
    same policy as any other. An observation that conflicts is therefore
    reported as the conflict it is, with its reason, rather than as an unknown.

    Parameters
    ----------
    request : PublicationRequest
        The attempt whose budget ran out.
    observed : ObservedRelease | None
        The newest reading the run holds, if any.
    span : OperationSpan
        Span that records how the run ended.
    publish_attempted : bool
        Whether any step of this run entered the publish call.

    Returns
    -------
    ReleaseDecision | None
        The `NOOP` decision the reading produced, or `None` when no write was
        attempted and the caller should report a state that did not converge.

    Raises
    ------
    ProverToolError
        Raised for an observed conflict, or for a publication whose write may
        have landed and whose outcome could not be confirmed.
    """
    if observed is not None:
        decision = decide(request.expected, observed, request.validation)
        if decision.action is ReleaseAction.NOOP:
            span.finish("noop")
            return decision
        if decision.action is ReleaseAction.CONFLICT:
            span.finish("conflict")
            msg = f"publication conflict: {decision.reason}"
            if publish_attempted:
                msg += (
                    " (observed after this run's publish call, which may have landed)"
                )
            raise ProverToolError(msg)
    if publish_attempted:
        span.finish("unconfirmed")
        msg = (
            "publication reached a publish call on its last allowed step, so the"
            " release may already be public; this run could not confirm it within"
            " its step budget, and a re-run will observe the release and drive it"
            " to a verified no-op"
        )
        raise ProverToolError(msg)
    return None
