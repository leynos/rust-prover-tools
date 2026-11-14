"""Correlation of a verified attestation with the workflow run it names.

A verifier proves that a bundle satisfies the policy; it does not prove that
the verified statement belongs to the run the manifest records. This module
compares the run a verified statement names with the recorded identity,
enforcing the run and the attempt separately so a rerun is never mistaken for
the build the manifest describes. Turning a provenance invocation into a run
is the attestation adapter's job, so this domain model stays free of the
verifier's URL syntax and names no external command in its refusals.
"""

from __future__ import annotations

import dataclasses as dc
import typing as typ

from rust_prover_tools.errors import ProverToolError

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from rust_prover_tools.kani_snapshot.models import WorkflowIdentity

__all__ = ("WorkflowRun", "require_workflow_run")


@dc.dataclass(frozen=True, slots=True)
class WorkflowRun:
    """The workflow run and attempt a verified invocation names.

    Attributes
    ----------
    run_id : int
        GitHub Actions run identifier.
    run_attempt : int
        Attempt within that run; a rerun has a higher attempt.
    """

    run_id: int
    run_attempt: int


def require_workflow_run(
    runs: cabc.Iterable[WorkflowRun],
    workflow: WorkflowIdentity,
) -> None:
    """Require a verified run to match the recorded workflow identity.

    The run and the attempt are enforced separately so a rerun is reported as
    a different attempt of the right run, not as an unrelated build. Both
    checks fail closed: an empty iterable satisfies neither.

    Parameters
    ----------
    runs : collections.abc.Iterable[WorkflowRun]
        Runs named by the verified records.
    workflow : WorkflowIdentity
        Run and attempt the manifest records.

    Raises
    ------
    ProverToolError
        Raised when no verified run names the recorded run, or when the
        recorded run appears only for another attempt.
    """
    verified = tuple(runs)
    if not any(run.run_id == workflow.run_id for run in verified):
        msg = (
            "verified provenance does not name the manifest's workflow run "
            f"{workflow.run_id}"
        )
        raise ProverToolError(msg)
    if not any(
        run == WorkflowRun(workflow.run_id, workflow.run_attempt) for run in verified
    ):
        msg = (
            f"verified provenance names workflow run {workflow.run_id} "
            f"for another attempt, not attempt {workflow.run_attempt}"
        )
        raise ProverToolError(msg)
