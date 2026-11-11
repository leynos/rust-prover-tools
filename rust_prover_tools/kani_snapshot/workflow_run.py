"""Correlation of a verified attestation with the workflow run it names.

`gh attestation verify` proves that a bundle satisfies the policy; it does not
prove that the verified statement belongs to the run the manifest records. This
module parses the SLSA provenance v1 invocation URL and compares it with the
recorded identity, enforcing the run and the attempt separately so a rerun is
never mistaken for the build the manifest describes.
"""

from __future__ import annotations

import dataclasses as dc
import typing as typ
import urllib.parse

from rust_prover_tools.errors import ProverToolError

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from rust_prover_tools.kani_snapshot.models import WorkflowIdentity

__all__ = ("WorkflowRun", "require_workflow_run")


def _run_attempt_segments(
    invocation: str,
    repository: str,
) -> tuple[str, str] | None:
    """Return the run and attempt segments of a canonical invocation URL.

    Parameters
    ----------
    invocation : str
        `invocationId` value from a verified provenance statement.
    repository : str
        `owner/repo` the invocation must belong to.

    Returns
    -------
    tuple[str, str] | None
        The run and attempt path segments, or `None` when the value is not
        this repository's canonical run-and-attempt URL.
    """
    parsed = urllib.parse.urlsplit(invocation)
    if parsed.scheme != "https" or parsed.hostname != "github.com":
        return None
    if parsed.query or parsed.fragment:
        return None
    parts = parsed.path.strip("/").split("/")
    prefix = [*repository.split("/"), "actions", "runs"]
    if parts[: len(prefix)] != prefix or len(parts) != len(prefix) + 3:
        return None
    if parts[len(prefix) + 1] != "attempts":
        return None
    return parts[len(prefix)], parts[len(prefix) + 2]


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

    @classmethod
    def from_invocation(cls, invocation: str, repository: str) -> WorkflowRun | None:
        """Parse one invocation URL into the run it names.

        Parameters
        ----------
        invocation : str
            `invocationId` value from a verified provenance statement.
        repository : str
            `owner/repo` the invocation must belong to.

        Returns
        -------
        WorkflowRun | None
            The named run, or `None` when the value is not this repository's
            canonical run-and-attempt URL. Malformed external data names no
            run rather than raising.
        """
        segments = _run_attempt_segments(invocation, repository)
        if segments is None:
            return None
        try:
            run_id, attempt = (int(segment) for segment in segments)
        except ValueError:
            return None
        if run_id <= 0 or attempt <= 0:
            return None
        return cls(run_id=run_id, run_attempt=attempt)


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
            "gh attestation verify did not verify the manifest's workflow run "
            f"{workflow.run_id}"
        )
        raise ProverToolError(msg)
    if not any(
        run == WorkflowRun(workflow.run_id, workflow.run_attempt) for run in verified
    ):
        msg = (
            f"gh attestation verify verified workflow run {workflow.run_id} "
            f"for another attempt, not attempt {workflow.run_attempt}"
        )
        raise ProverToolError(msg)
