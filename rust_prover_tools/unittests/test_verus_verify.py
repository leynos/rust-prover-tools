"""Tests for Verus proof-batch verification and failure reporting."""

from __future__ import annotations

import logging
import pathlib
import typing as typ

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

import rust_prover_tools.verus.batch as verus_batch_module
from rust_prover_tools.commands import CommandResult, CommandSpec
from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.verus import (
    ProofFailureContext,
    VerusProofFailedError,
    VerusRunSettings,
    verify_proof_files,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc


def test_verify_proof_files_keeps_partial_result_when_invocation_fails(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An early invocation failure still names the files already proved."""
    proved_file = tmp_path / "proved.rs"
    unstartable_file = tmp_path / "unstartable.rs"

    def fake_run_command(spec: CommandSpec) -> CommandResult:
        if spec.argv[1] == str(proved_file):
            return CommandResult(stdout="", stderr="", exit_code=0)
        msg = "verifier disappeared"
        raise FileNotFoundError(msg)

    monkeypatch.setattr(verus_batch_module, "run_command", fake_run_command)

    with pytest.raises(ProverToolError, match="Verus invocation failed") as exc_info:
        verify_proof_files(
            (proved_file, unstartable_file),
            VerusRunSettings(pathlib.Path("/bin/verus"), "toolchain"),
        )

    message = str(exc_info.value)
    assert f"Proof file: {unstartable_file}" in message, (
        "Invocation failure should name the file that could not run"
    )
    assert f"Proved before the failure: {proved_file}" in message, (
        "Invocation failure should keep the files proved before it"
    )


def test_failure_message_quotes_unsafe_paths(tmp_path: pathlib.Path) -> None:
    """Unsafe path text cannot spoof or split the failure report."""
    unsafe_file = tmp_path / "bad, name\nspoof.rs"
    context = ProofFailureContext(
        7,
        pathlib.Path("/bin/verus"),
        unsafe_file,
        "toolchain",
        (tmp_path / "first.rs",),
    )

    message = str(VerusProofFailedError(context))

    assert repr(str(unsafe_file)) in message, (
        "A path with a newline or list delimiter should be quoted"
    )
    assert "\nspoof.rs" not in message, (
        "A newline in a path must not inject a new diagnostic line"
    )


def test_verify_proof_files_emits_bounded_events(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Proof-batch telemetry carries counters and classifications, not paths."""
    proof_files = (pathlib.Path("first.rs"), pathlib.Path("second.rs"))

    def fake_run_command(_spec: object) -> CommandResult:
        return CommandResult(stdout="", stderr="", exit_code=0)

    monkeypatch.setattr(verus_batch_module, "run_command", fake_run_command)

    with caplog.at_level(logging.INFO, logger="rust_prover_tools.verus.batch"):
        verify_proof_files(
            proof_files,
            VerusRunSettings(pathlib.Path("/bin/verus"), "toolchain"),
        )

    events = [
        record
        for record in caplog.records
        if record.name == "rust_prover_tools.verus.batch"
    ]
    assert [getattr(event, "operation", None) for event in events] == [
        "verus-proof-batch-start",
        "verus-proof-invocation-start",
        "verus-proof-invocation",
        "verus-proof-invocation-start",
        "verus-proof-invocation",
        "verus-proof-batch",
    ], "The batch and each invocation should emit paired start and completion events"
    assert getattr(events[-1], "proof_count", None) == 2, (
        "The batch event should report both requested proof files"
    )
    assert getattr(events[-1], "proved_count", None) == 2, (
        "The batch event should report both proof files as proved"
    )
    for event in events:
        payload = str(vars(event))
        assert "first.rs" not in payload, "Telemetry must not carry proof paths"
        assert "second.rs" not in payload, "Telemetry must not carry proof paths"


def test_verify_proof_files_uses_injected_clock(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Telemetry durations come from the injected clock, not wall time."""
    proof_files = (pathlib.Path("first.rs"), pathlib.Path("second.rs"))
    ticks = iter([0.0, 1.0, 2.0, 3.0, 5.0, 6.0])

    def fake_run_command(_spec: object) -> CommandResult:
        return CommandResult(stdout="", stderr="", exit_code=0)

    monkeypatch.setattr(verus_batch_module, "run_command", fake_run_command)

    with caplog.at_level(logging.INFO, logger="rust_prover_tools.verus.batch"):
        verify_proof_files(
            proof_files,
            VerusRunSettings(
                pathlib.Path("/bin/verus"),
                "toolchain",
                clock=lambda: next(ticks),
            ),
        )

    durations = [
        getattr(record, "duration_seconds", None)
        for record in caplog.records
        if getattr(record, "operation", None) == "verus-proof-invocation"
    ]
    assert durations == pytest.approx([1.0, 2.0]), (
        "Each invocation duration should be measured with the injected clock"
    )
    batch_events = [
        record
        for record in caplog.records
        if getattr(record, "operation", None) == "verus-proof-batch"
    ]
    assert getattr(batch_events[-1], "duration_seconds", None) == pytest.approx(6.0), (
        "The batch duration should span the injected clock's readings"
    )


def test_verify_proof_files_reports_unexpected_failure(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An exception outside the handled paths still reports a failed batch."""
    proof_files = (pathlib.Path("first.rs"),)

    def fake_run_command(_spec: object) -> CommandResult:
        msg = "verifier wrapper crashed"
        raise RuntimeError(msg)

    monkeypatch.setattr(verus_batch_module, "run_command", fake_run_command)

    with (
        caplog.at_level(logging.INFO, logger="rust_prover_tools.verus.batch"),
        pytest.raises(RuntimeError, match="verifier wrapper crashed"),
    ):
        verify_proof_files(
            proof_files,
            VerusRunSettings(pathlib.Path("/bin/verus"), "toolchain"),
        )

    operations = [getattr(record, "operation", None) for record in caplog.records]
    assert operations == [
        "verus-proof-batch-start",
        "verus-proof-invocation-start",
        "verus-proof-invocation",
        "verus-proof-batch",
    ], "An unhandled exception should still close the invocation span"

    invocation_events = [
        record
        for record in caplog.records
        if getattr(record, "operation", None) == "verus-proof-invocation"
    ]
    assert getattr(invocation_events[-1], "status_class", None) == "failure", (
        "An unhandled exception should close the invocation as a failure"
    )
    assert getattr(invocation_events[-1], "exit_code", "missing") is None, (
        "An unhandled exception has no verifier exit code"
    )
    assert getattr(invocation_events[-1], "error_category", None) == "unexpected", (
        "An unhandled exception should carry the unexpected error category"
    )

    batch_events = [
        record
        for record in caplog.records
        if getattr(record, "operation", None) == "verus-proof-batch"
    ]
    assert getattr(batch_events[-1], "status_class", None) == "failure", (
        "An unhandled exception must not be reported as a successful batch"
    )
    assert getattr(batch_events[-1], "error_category", None) == "unexpected", (
        "An unhandled exception should carry the unexpected error category"
    )


_EXPECTED_FAILURE_CODE = 9


def _proof_settings() -> VerusRunSettings:
    """Return settings that pass one extra argument to each invocation."""
    return VerusRunSettings(
        pathlib.Path("/bin/verus"),
        "toolchain",
        ("--extra",),
    )


def _fake_verifier(
    proof_files: tuple[pathlib.Path, ...],
    failing_index: int | None,
    invocations: list[tuple[str, ...]],
) -> cabc.Callable[[CommandSpec], CommandResult]:
    """Return a verifier stub that records calls and fails at one index."""

    def fake_run_command(spec: CommandSpec) -> CommandResult:
        invocations.append(spec.argv)
        proof_file = pathlib.Path(spec.argv[1])
        exit_code = (
            _EXPECTED_FAILURE_CODE
            if failing_index is not None and proof_file == proof_files[failing_index]
            else 0
        )
        return CommandResult(stdout="", stderr="", exit_code=exit_code)

    return fake_run_command


def _assert_successful_batch(
    proof_files: tuple[pathlib.Path, ...],
    invocations: list[tuple[str, ...]],
) -> None:
    """Assert every requested file ran once, in order."""
    assert [argv[1] for argv in invocations] == [str(path) for path in proof_files], (
        "A successful batch should invoke every file in order"
    )


def _assert_failed_batch(
    proof_files: tuple[pathlib.Path, ...],
    failing_index: int,
    exc_info: pytest.ExceptionInfo[VerusProofFailedError],
    invocations: list[tuple[str, ...]],
) -> None:
    """Assert the batch stopped at the failing file and named it."""
    expected_prefix = proof_files[: failing_index + 1]
    assert [argv[1] for argv in invocations] == [
        str(path) for path in expected_prefix
    ], "Invocation should stop at the first failing file"
    assert all(argv[2:] == ("--extra",) for argv in invocations), (
        "Extra arguments should accompany every invocation"
    )
    assert exc_info.value.exit_code == _EXPECTED_FAILURE_CODE, (
        "The failing invocation should set the code"
    )
    message = str(exc_info.value)
    assert f"Proof file: {proof_files[failing_index]}" in message, (
        "The failure should name the file that failed"
    )
    earlier = proof_files[:failing_index]
    if earlier:
        listed = ", ".join(str(path) for path in earlier)
        assert f"Proved before the failure: {listed}" in message, (
            "The failure should report exactly the earlier proved files"
        )
    else:
        assert "Proved before the failure" not in message, (
            "A first-file failure has no earlier proved files to report"
        )


@given(
    names=st.lists(
        st.text(alphabet="abcdefghijklmnopqrstuvwxyz_", min_size=1, max_size=8),
        min_size=1,
        max_size=5,
        unique=True,
    ),
    failure_position=st.integers(min_value=-1, max_value=4),
)
@settings(
    max_examples=50,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_verify_proof_files_runs_an_ordered_prefix(
    names: list[str],
    failure_position: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """For any sequence and failure point, only the ordered prefix runs."""
    proof_files = tuple(pathlib.Path(f"{name}.rs") for name in names)
    failing_index = (
        None if failure_position == -1 else failure_position % len(proof_files)
    )
    invocations: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        verus_batch_module,
        "run_command",
        _fake_verifier(proof_files, failing_index, invocations),
    )

    if failing_index is None:
        verify_proof_files(proof_files, _proof_settings())
        _assert_successful_batch(proof_files, invocations)
        return

    with pytest.raises(VerusProofFailedError) as exc_info:
        verify_proof_files(proof_files, _proof_settings())

    _assert_failed_batch(proof_files, failing_index, exc_info, invocations)
