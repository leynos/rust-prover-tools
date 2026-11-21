"""The entrypoint sends the library's events to the job's run log.

The library installs no handlers, so a hosted job would otherwise report only
the final error: a transient retry, a publication decision or the ending of a
publication would be invisible without a local rerun. The entrypoint is the one
place that opts in. These tests drive publication through `main` against
in-memory release APIs and assert the events reach standard error.
"""

from __future__ import annotations

import logging
import typing as typ

from rust_prover_tools.kani_snapshot.orchestrator import entrypoint
from rust_prover_tools.kani_snapshot.publish import publish_release
from rust_prover_tools.unittests import (
    kani_snapshot_entrypoint_logging_fixtures as fixtures,
)
from tests import kani_publish_fixtures

if typ.TYPE_CHECKING:
    import pathlib

    import pytest

    from rust_prover_tools.kani_snapshot.publish_request import PublicationRequest
    from rust_prover_tools.kani_snapshot.release_port import ReleaseApi


def _publish_subcommand(
    monkeypatch: pytest.MonkeyPatch,
    request: PublicationRequest,
    api: ReleaseApi,
    max_steps: int,
) -> None:
    """Replace the composition root so the test chooses the release API."""

    def handler(_namespace: object) -> int:
        """Publish against the release API the test supplied."""
        publish_release(request, api, max_steps=max_steps)
        return 0

    monkeypatch.setattr(entrypoint, "_publish_with_github", handler)


def _run_publish() -> int:
    """Run the publish subcommand with the run identity every job supplies."""
    return entrypoint.main([
        "publish",
        *fixtures.run_flags(),
        "--manifest",
        "manifest.json",
        "--receipt",
        "receipt.json",
        "--payloads",
        "payloads",
    ])


def test_a_transient_step_reaches_the_run_log(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A retryable failure is visible without lowering a logger by hand.

    The upload fails transiently once, so the run records the decision that
    asked for it and the transient event for the action it retried. Both must
    reach standard error, because the run log is the only place a maintainer
    will look.
    """
    api = fixtures.TransientUploadApi()
    _publish_subcommand(monkeypatch, kani_publish_fixtures.request(tmp_path), api, 8)

    assert _run_publish() == 0, "the publication did not converge"

    err = capsys.readouterr().err
    assert "kani.snapshot.publish.decision" in err, (
        "the decision event did not reach the run log"
    )
    assert "kani.snapshot.publish.transient" in err, (
        "the transient event did not reach the run log"
    )
    assert "kani.snapshot.publish.complete" in err, (
        "the operation's completion event did not reach the run log"
    )


def test_a_state_that_never_converged_reaches_the_run_log(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A spent budget reports its outcome, not only the exception class.

    Every step creates a draft the next observation cannot see, so publication
    runs out of steps. The failure event must carry that outcome beside the
    exception's class name, which is the difference between "it broke" and "it
    ran out of steps".
    """
    api = fixtures.PhantomDraftApi()
    _publish_subcommand(monkeypatch, kani_publish_fixtures.request(tmp_path), api, 3)

    assert _run_publish() == 1, "the failure was not reported to the job"

    err = capsys.readouterr().err
    assert "kani.snapshot.publish.failed" in err, (
        "the failure event did not reach the run log"
    )
    assert "outcome=not_converged" in err, (
        "the span's decided outcome was not reported with the failure"
    )
    assert "reason=ProverToolError" in err, (
        "the exception's class was not reported with the failure"
    )
    assert "kani-snapshot-producer:" in err, "the user-facing error line was lost"


def test_a_decision_reports_the_step_and_action(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The decision event carries the fields a maintainer needs.

    A bare event name would say a step happened without saying which step or
    what it did, so the formatter must render the bounded fields too.
    """
    api = fixtures.TransientUploadApi()
    _publish_subcommand(monkeypatch, kani_publish_fixtures.request(tmp_path), api, 8)

    assert _run_publish() == 0, "the publication did not converge"

    err = capsys.readouterr().err
    assert "kani.snapshot.publish.decision step=1" in err, (
        "the decision event did not report its step"
    )
    assert "action=upload-assets" in err, "the decision event did not report its action"
    assert "kani.snapshot.publish.transient action=upload-assets" in err, (
        "the transient event did not report the action it retried"
    )


def test_configuring_logging_twice_adds_one_handler() -> None:
    """Repeated invocation must not duplicate every event line.

    `main` is called once per subcommand in the workflow, but tests and
    embedding callers may call it more than once in the same process.
    """
    entrypoint._configure_logging()
    logger = logging.getLogger("rust_prover_tools")
    first = sum(
        isinstance(handler, entrypoint._StderrEvents) for handler in logger.handlers
    )
    entrypoint._configure_logging()
    second = sum(
        isinstance(handler, entrypoint._StderrEvents) for handler in logger.handlers
    )

    assert second == first == 1, "the entrypoint installed a duplicate event handler"
    assert logger.level == logging.DEBUG, "the event level was not enabled"
