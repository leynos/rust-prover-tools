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
    from pathlib import Path

_LOGGER = logging.getLogger(__name__)


def _outcome_class(exit_code: int) -> str:
    """Describe a verifier exit status without carrying its output."""
    if exit_code == 0:
        return "success"
    return "signal" if exit_code < 0 else "nonzero"


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


def _emit_invocation(event: _InvocationEvent) -> None:
    """Write one invocation event to this module's logger."""
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
    verus_bin: Path
    toolchain: str
    extra_args: tuple[str, ...]
    proved: list[Path] = dc.field(default_factory=list)
    status_class: str = "success"
    error_category: str = "none"
    started_at: float = dc.field(default_factory=time.monotonic)

    def run(self) -> None:
        """Verify each file in order, stopping at the first failure."""
        count = len(self.proof_files)
        for ordinal, proof_file in enumerate(self.proof_files, start=1):
            started_at = time.monotonic()
            try:
                result = _invoke(self.verus_bin, proof_file, self.extra_args)
            except OSError as exc:
                self.status_class, self.error_category = "failure", "execution"
                failure_event = _InvocationEvent(
                    ordinal=ordinal,
                    count=count,
                    status_class=self.status_class,
                    exit_code=None,
                    error_category=self.error_category,
                    duration_seconds=time.monotonic() - started_at,
                )
                _emit_invocation(failure_event)
                raise _partial_run_error(self.proved, proof_file, exc) from exc

            status = _outcome_class(result.exit_code)
            category = "none" if status == "success" else "command"
            result_event = _InvocationEvent(
                ordinal=ordinal,
                count=count,
                status_class=status,
                exit_code=result.exit_code,
                error_category=category,
                duration_seconds=time.monotonic() - started_at,
            )
            _emit_invocation(result_event)
            if result.exit_code != 0:
                self.status_class, self.error_category = status, category
                context = ProofFailureContext(
                    result.exit_code,
                    self.verus_bin,
                    proof_file,
                    self.toolchain,
                    tuple(self.proved),
                )
                raise VerusProofFailedError(context)
            self.proved.append(proof_file)

    def emit_summary(self) -> None:
        """Emit the closing telemetry event for this batch."""
        event = _BatchEvent(
            count=len(self.proof_files),
            proved_count=len(self.proved),
            status_class=self.status_class,
            error_category=self.error_category,
            duration_seconds=time.monotonic() - self.started_at,
        )
        _emit_batch(event)


def verify_proof_files(
    proof_files: tuple[Path, ...],
    verus_bin: Path,
    toolchain: str,
    extra_args: tuple[str, ...],
) -> None:
    """Verify each proof file in order, stopping at the first failure.

    Parameters
    ----------
    proof_files : tuple[Path, ...]
        Proof files to verify, already checked for existence.
    verus_bin : Path
        Resolved Verus executable.
    toolchain : str
        Rust toolchain identifier, reported on failure.
    extra_args : tuple[str, ...]
        Extra verifier arguments appended after each proof file.

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
    Each completed invocation emits one INFO event carrying only bounded
    fields, and the batch emits one closing event. The events never include
    proof paths.
    """
    batch = _ProofBatch(proof_files, verus_bin, toolchain, extra_args)
    try:
        batch.run()
    finally:
        batch.emit_summary()
