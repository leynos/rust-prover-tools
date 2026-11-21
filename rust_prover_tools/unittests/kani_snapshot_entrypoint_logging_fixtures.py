"""Doubles the entrypoint logging tests need, kept out of the test module.

Publication is driven end to end through `main`, so the release API has to
behave like a real one for several steps. Two doubles cover the endings the
logging tests care about: a step that fails transiently and is retried, and an
API that never answers an observation at all.
"""

from __future__ import annotations

from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_snapshot.models import REPOSITORY
from rust_prover_tools.kani_snapshot.release_model import ObservedRelease
from tests.kani_release_api_fixtures import FakeReleaseApi


def run_flags(run_id: int = 12345, run_attempt: int = 1) -> list[str]:
    """Return the trusted run identity every producer subcommand requires.

    Parameters
    ----------
    run_id : int
        Workflow run the operation belongs to.
    run_attempt : int
        Attempt within that run.

    Returns
    -------
    list[str]
        Arguments naming the run, its attempt, the event and its URL.
    """
    return [
        "--run-id",
        str(run_id),
        "--run-attempt",
        str(run_attempt),
        "--event",
        "workflow_dispatch",
        "--run-url",
        f"https://github.com/{REPOSITORY}/actions/runs/{run_id}",
    ]


class TransientUploadApi(FakeReleaseApi):
    """Fail the first upload transiently, then behave.

    One retryable failure is enough to emit both the decision that asked for
    the upload and the transient event recording it.
    """

    def __init__(self) -> None:
        """Create the fake with one upload failure scheduled."""
        super().__init__()
        self.fail_next("upload")


class PhantomDraftApi(FakeReleaseApi):
    """Accept a draft and then report that no release exists.

    Every step creates a draft the next observation cannot see, so the run
    spends its whole budget repeating the same action and never converges. No
    publish call is made, which makes this the ending that reports a state that
    did not converge.
    """

    def observe(self, tag: str) -> ObservedRelease:
        """Report an absent release however much state the fake holds."""
        self.calls.append("observe")
        return ObservedRelease(exists=False, is_draft=False)


class NeverObservableApi(FakeReleaseApi):
    """Fail every observation permanently, so no step is ever decided.

    A permanent failure is not retried, so the run ends on the failure rather
    than reporting a state that did not converge.
    """

    def observe(self, tag: str) -> ObservedRelease:
        """Fail the read rather than returning a state."""
        self.calls.append("observe")
        msg = "the release API is unavailable"
        raise ProverToolError(msg)
