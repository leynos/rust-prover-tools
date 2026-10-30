"""Ordered, fail-fast execution of Verus proof files.

Verus accepts one input filename per invocation, so a multi-file request is a
sequence of verifier runs. This module owns that sequence, the partial result
reported when a later file fails, and the bounded INFO telemetry emitted around
each invocation. Binary resolution and toolchain management stay in
`rust_prover_tools.verus.run`.

Telemetry carries counters, classifications, and durations only. Proof paths,
arguments, and verifier output stay out of the events so log aggregation cannot
record user file names.
"""

from __future__ import annotations

import dataclasses as dc
import logging
import time
import typing as typ

from rust_prover_tools.commands import CommandResult, CommandSpec, run_command
from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.verus.models import (
    ProofFailureContext,
    VerusProofFailedError,
    display_proof_path,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

_LOGGER = logging.getLogger(__name__)


@dc.dataclass(frozen=True, slots=True)
class VerusRunSettings:
    """Fixed inputs shared by every invocation in one proof batch.

    Attributes
    ----------
    verus_bin : Path
        Resolved Verus executable.
    toolchain : str
        Rust toolchain identifier, reported on failure.
    extra_args : tuple[str, ...]
        Extra verifier arguments appended after each proof file.
    clock : Callable[[], float]
        Monotonic time source for telemetry durations. Tests inject a
        deterministic fake; production callers keep the default.
    """

    verus_bin: Path
    toolchain: str
    extra_args: tuple[str, ...] = ()
    clock: cabc.Callable[[], float] = time.monotonic


def _outcome_class(exit_code: int) -> str:
    """Describe a verifier exit status without carrying its output."""
    if exit_code == 0:
        return "success"
    return "signal" if exit_code < 0 else "nonzero"


@dc.dataclass(frozen=True, slots=True)
class _InvocationStart:
    """Bounded telemetry emitted when a verifier invocation begins."""

    ordinal: int
    count: int


@dc.dataclass(frozen=True, slots=True)
class _InvocationEvent:
    """Bounded telemetry for one completed verifier invocation.

    INFO keeps the normal CLI output unchanged unless the application enables
    logging. Proof paths never appear here, only counters and classifications.
    """

    ordinal: int
    count: int
    status_class: str
    exit_code: int | None
    error_category: str
    duration_seconds: float


@dc.dataclass(frozen=True, slots=True)
class _BatchEvent:
    """Bounded telemetry for a whole proof batch."""

    count: int
    proved_count: int
    status_class: str
    error_category: str
    duration_seconds: float


def _emit_invocation_start(event: _InvocationStart) -> None:
    """Write the start of one invocation span to this module's logger."""
    _LOGGER.info(
        "Verus proof invocation started",
        extra={
            "operation": "verus-proof-invocation-start",
            "proof_ordinal": event.ordinal,
            "proof_count": event.count,
        },
    )


def _emit_invocation(event: _InvocationEvent) -> None:
    """Write one invocation completion event to this module's logger."""
    _LOGGER.info(
        "Verus proof invocation completed",
        extra={
            "operation": "verus-proof-invocation",
            "proof_ordinal": event.ordinal,
            "proof_count": event.count,
            "status_class": event.status_class,
            "exit_code": event.exit_code,
            "error_category": event.error_category,
            "duration_seconds": event.duration_seconds,
        },
    )


def _emit_batch_start(count: int) -> None:
    """Write the start of the batch span to this module's logger."""
    _LOGGER.info(
        "Verus proof batch started",
        extra={"operation": "verus-proof-batch-start", "proof_count": count},
    )


def _emit_batch(event: _BatchEvent) -> None:
    """Write the closing batch event to this module's logger."""
    _LOGGER.info(
        "Verus proof batch completed",
        extra={
            "operation": "verus-proof-batch",
            "proof_count": event.count,
            "proved_count": event.proved_count,
            "status_class": event.status_class,
            "error_category": event.error_category,
            "duration_seconds": event.duration_seconds,
        },
    )


def _partial_run_error(
    proved: list[Path],
    proof_file: Path,
    error: OSError,
) -> ProverToolError:
    """Build an error that keeps a partial result when invocation fails early."""
    message = (
        "Verus invocation failed before returning a result.\n"
        f"Proof file: {display_proof_path(proof_file)}\n"
        f"Error: {error}"
    )
    if proved:
        listed = ", ".join(display_proof_path(path) for path in proved)
        message += f"\nProved before the failure: {listed}"
    return ProverToolError(message)


def _invoke(
    verus_bin: Path,
    proof_file: Path,
    extra_args: tuple[str, ...],
) -> CommandResult:
    """Run the verifier once for a single proof file."""
    command = (str(verus_bin), str(proof_file), *extra_args)
    return run_command(CommandSpec(command, echo=True))


@dc.dataclass(slots=True)
class _ProofBatch:
    """One ordered, fail-fast batch of single-file verifier invocations."""

    proof_files: tuple[Path, ...]
    settings: VerusRunSettings
    started_at: float
    proved: list[Path] = dc.field(default_factory=list)
    # Failure by default: `run` sets success only after the whole loop
    # finishes, so an interrupted or unexpected abort cannot look successful.
    status_class: str = "failure"
    error_category: str = "unexpected"

    def run(self) -> None:
        """Verify each file in order, stopping at the first failure."""
        count = len(self.proof_files)
        _emit_batch_start(count)
        for ordinal, proof_file in enumerate(self.proof_files, start=1):
            self._run_one(ordinal, proof_file, count)
        self.status_class, self.error_category = "success", "none"

    def _run_one(self, ordinal: int, proof_file: Path, count: int) -> None:
        """Run one invocation and close its span on every exit path.

        The completion event defaults to an unexpected failure because the
        `finally` emits it even when a handler re-raises. An exception outside
        the handled paths therefore closes the span instead of leaving an
        unmatched start that would read as a stalled invocation.
        """
        started_at = self.settings.clock()
        _emit_invocation_start(_InvocationStart(ordinal=ordinal, count=count))
        status_class = "failure"
        exit_code: int | None = None
        error_category = "unexpected"
        try:
            result = _invoke(
                self.settings.verus_bin,
                proof_file,
                self.settings.extra_args,
            )
        except OSError as exc:
            self.status_class, self.error_category = "failure", "execution"
            error_category = "execution"
            raise _partial_run_error(self.proved, proof_file, exc) from exc
        else:
            status_class = _outcome_class(result.exit_code)
            exit_code = result.exit_code
            error_category = "none" if status_class == "success" else "command"
            if result.exit_code != 0:
                self.status_class, self.error_category = status_class, error_category
                context = ProofFailureContext(
                    result.exit_code,
                    self.settings.verus_bin,
                    proof_file,
                    self.settings.toolchain,
                    tuple(self.proved),
                )
                raise VerusProofFailedError(context)
            self.proved.append(proof_file)
        finally:
            _emit_invocation(
                _InvocationEvent(
                    ordinal=ordinal,
                    count=count,
                    status_class=status_class,
                    exit_code=exit_code,
                    error_category=error_category,
                    duration_seconds=self.settings.clock() - started_at,
                )
            )

    def emit_summary(self) -> None:
        """Emit the closing telemetry event for this batch."""
        event = _BatchEvent(
            count=len(self.proof_files),
            proved_count=len(self.proved),
            status_class=self.status_class,
            error_category=self.error_category,
            duration_seconds=self.settings.clock() - self.started_at,
        )
        _emit_batch(event)


def verify_proof_files(
    proof_files: tuple[Path, ...],
    settings: VerusRunSettings,
) -> None:
    """Verify each proof file in order, stopping at the first failure.

    Parameters
    ----------
    proof_files : tuple[Path, ...]
        Proof files to verify, already checked for existence.
    settings : VerusRunSettings
        Resolved executable, toolchain, extra arguments, and timing source
        shared by every invocation in the batch.

    Raises
    ------
    VerusProofFailedError
        Raised on the first non-zero verifier exit. The context names the file
        that failed and the files proved before it, and preserves the exit
        code.
    ProverToolError
        Raised when the verifier cannot be started for a later file; the
        message still names the files proved before that failure.

    Notes
    -----
    The batch and each invocation emit a start event followed by a completion
    event, so a stalled invocation is visible as an unmatched start. Every
    event carries only bounded counters, classifications, and durations; none
    includes proof paths. A batch that aborts without a handled failure
    reports `failure` / `unexpected` rather than a misleading success.
    """
    batch = _ProofBatch(proof_files, settings, settings.clock())
    try:
        batch.run()
    finally:
        batch.emit_summary()
