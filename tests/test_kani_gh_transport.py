"""Transport classification and budgets for the shared `gh` wrapper.

A publication is only safe if every `gh` failure is classified once, in one
place: a connection loss must be retried, an authentication refusal must stop
the run, and an absent release must be confirmed by an HTTP 404 rather than by
any wording that happens to contain "not found". The wrapper also owns the
wall-clock budget, so a hung read cannot outlive the run and a timed-out write
stays retryable because it may still have landed.
"""

from __future__ import annotations

import logging
import typing as typ

import pytest

from rust_prover_tools.commands import (
    CommandResult,
    CommandSpec,
    CommandTimeoutError,
)
from rust_prover_tools.kani_snapshot import gh_transport
from rust_prover_tools.kani_snapshot.gh_transport import (
    READ_TIMEOUT_SECONDS,
    TRANSFER_TIMEOUT_SECONDS,
    failed,
    gh,
    gh_optional,
    is_not_found,
)
from rust_prover_tools.kani_snapshot.release_model import ReleaseTransientError

if typ.TYPE_CHECKING:
    import pathlib

#: A timeout message the command runner would produce for a timed-out `gh`.
_TIMEOUT_MESSAGE: typ.Final[str] = (
    "command timed out after 1 seconds: gh api repos/owner/repo/releases"
)


@pytest.mark.parametrize(
    "stderr",
    [
        pytest.param("gh: connection reset by peer", id="connection-reset"),
        pytest.param("gh: connection refused", id="connection-refused"),
        pytest.param("gh: network is unreachable", id="network-unreachable"),
        pytest.param("gh: TLS handshake timeout", id="tls-handshake-timeout"),
        pytest.param("gh: unexpected EOF", id="unexpected-eof"),
        pytest.param("gh: i/o timeout", id="io-timeout"),
    ],
)
def test_connection_loss_is_classified_as_transient(stderr: str) -> None:
    """A transport-shaped failure may reconcile on a fresh observation."""
    assert isinstance(failed(1, stderr), ReleaseTransientError)


@pytest.mark.parametrize(
    "stderr",
    [
        pytest.param("gh: HTTP 401: Bad credentials (authentication failed)"),
        pytest.param("gh: HTTP 403: Resource not accessible (permission denied)"),
        pytest.param("gh: authentication failed: token expired"),
    ],
)
def test_authentication_refusals_are_permanent(stderr: str) -> None:
    """A refused identity or permission never becomes retryable."""
    error = failed(1, stderr)
    assert not isinstance(error, ReleaseTransientError)


def test_an_auth_refusal_stays_permanent_when_its_text_mentions_timeout() -> None:
    """An explicit refusal outranks a generic word that usually means retry.

    `gh` can print "authentication timeout" when an SSO exchange is slow, but a
    401 still describes a refused identity rather than a busy server, so
    retrying the same credentials cannot reconcile it.
    """
    error = failed(1, "gh: HTTP 401: authentication timeout")
    assert not isinstance(error, ReleaseTransientError)


def test_a_download_pattern_that_matches_nothing_is_transient() -> None:
    """A listing that no longer matches the pattern is a moved release.

    `gh release download --pattern NAME` fails with this exact wording when
    the named asset is not in the listing it just read. The read is a race with
    a concurrent writer, not a verdict about the release, so it must be
    re-observed rather than reported as a permanent failure. Pinned to the
    real message so a future `gh` release that rewords it fails here.
    """
    assert isinstance(
        failed(1, 'no assets match the file pattern "launcher.tar.gz"'),
        ReleaseTransientError,
    )


def test_is_not_found_requires_a_confirmed_404() -> None:
    """Only a 404 is absence; a 503 that says "not found" is a failure."""
    assert is_not_found("gh: HTTP 404: Not Found")
    assert not is_not_found("gh: HTTP 503: Service Unavailable: release not found")


def test_gh_translates_a_command_timeout_into_a_transient_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A timed-out write may still have landed, so it must be re-observed."""
    spec = None

    def _timed_out(command: object) -> CommandResult:
        """Fail exactly as a command that outlived its budget would."""
        nonlocal spec
        spec = command
        raise CommandTimeoutError(_TIMEOUT_MESSAGE)

    monkeypatch.setattr(gh_transport, "run_command", _timed_out)

    with pytest.raises(ReleaseTransientError, match="timed out"):
        gh(("gh", "api", "repos/owner/repo/releases"))


def test_a_timeout_reports_the_kind_without_an_exit_code(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A timed-out command never produced an exit status to report.

    The event carries `kind="timeout"` and no `exit_code`, which is what the
    developer's guide documents; a consumer requiring both fields would
    otherwise fail to process it.
    """

    def _timed_out(_spec: object) -> CommandResult:
        """Fail exactly as a command that outlived its budget would."""
        raise CommandTimeoutError(_TIMEOUT_MESSAGE)

    monkeypatch.setattr(gh_transport, "run_command", _timed_out)

    with (
        caplog.at_level(logging.DEBUG, logger=gh_transport.__name__),
        pytest.raises(ReleaseTransientError),
    ):
        gh(("gh", "api", "repos/owner/repo/releases"))

    events = [
        record.event_data for record in caplog.records if hasattr(record, "event_data")
    ]
    assert events == [{"event": "kani.snapshot.gh.failed", "kind": "timeout"}], (
        "a timeout reported an exit code it never received"
    )


def test_gh_gives_a_read_the_read_budget_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unbounded read could outlive the whole publication run."""
    captured: list[CommandSpec] = []

    def _succeed(spec: CommandSpec) -> CommandResult:
        """Record the specification and report success."""
        captured.append(spec)
        return CommandResult(stdout="", stderr="", exit_code=0)

    monkeypatch.setattr(gh_transport, "run_command", _succeed)

    gh(("gh", "api", "repos/owner/repo/releases"))

    assert len(captured) == 1
    assert captured[0].timeout_seconds == READ_TIMEOUT_SECONDS


def test_a_failed_call_is_reported_with_a_bounded_classification(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The event carries the category and exit status, never the message."""
    stderr = "gh: HTTP 503: Service Unavailable: repos/owner/repo/releases"

    def _fail(spec: object) -> CommandResult:
        """Answer with a retryable server failure."""
        return CommandResult(stdout="", stderr=stderr, exit_code=1)

    monkeypatch.setattr(gh_transport, "run_command", _fail)

    with (
        caplog.at_level(logging.DEBUG, logger=gh_transport.__name__),
        pytest.raises(ReleaseTransientError),
    ):
        gh(("gh", "api", "repos/owner/repo/releases"))

    events = [
        record.event_data for record in caplog.records if hasattr(record, "event_data")
    ]
    assert events == [
        {"event": "kani.snapshot.gh.failed", "exit_code": 1, "kind": "transient"}
    ]
    assert "repos/owner/repo" not in str(events)


def test_an_absent_release_is_reported_as_absence(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A confirmed 404 is a distinct category, not a failure to retry."""

    def _absent(spec: object) -> CommandResult:
        """Answer with a confirmed 404."""
        return CommandResult(stdout="", stderr="gh: HTTP 404: Not Found", exit_code=1)

    monkeypatch.setattr(gh_transport, "run_command", _absent)

    with caplog.at_level(logging.DEBUG, logger=gh_transport.__name__):
        assert gh_optional(("gh", "api", "repos/owner/repo/releases/tags/v1")) is None

    events = [
        record.event_data for record in caplog.records if hasattr(record, "event_data")
    ]
    assert events == [{"event": "kani.snapshot.gh.absent", "exit_code": 1}]


def test_gh_accepts_an_explicit_transfer_budget(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    """Downloads and uploads need a longer budget than a metadata read."""
    captured: list[CommandSpec] = []

    def _succeed(spec: CommandSpec) -> CommandResult:
        """Record the specification and report success."""
        captured.append(spec)
        return CommandResult(stdout="", stderr="", exit_code=0)

    monkeypatch.setattr(gh_transport, "run_command", _succeed)

    gh(
        ("gh", "release", "download", "v1", "--dir", str(tmp_path / "out")),
        timeout_seconds=TRANSFER_TIMEOUT_SECONDS,
    )

    assert captured[0].timeout_seconds == TRANSFER_TIMEOUT_SECONDS
    assert READ_TIMEOUT_SECONDS < TRANSFER_TIMEOUT_SECONDS
