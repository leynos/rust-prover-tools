"""Adapt a captured Verus log and its expectation to the command line.

Analysis reads a transcript, so it is separate from ``verus run``, which
produces one: the analysis works anywhere a log exists, without a Verus
installation or a proof directory.  Only the log is bound to the environment;
inherited ``INPUT_`` configuration is disabled, because a stray variable must
not silently change which log is analysed.
"""

from __future__ import annotations

import json
import sys
import typing as typ
from pathlib import Path  # ruff: ignore[typing-only-standard-library-import] - Cyclopts evaluates annotations

from cyclopts import Parameter

from rust_prover_tools.cli_support import InputError, read_input_text
from rust_prover_tools.exit_status import print_lines
from rust_prover_tools.verus_log import (
    FULL_RUN,
    FailureExpectation,
    VerusAnalysis,
    VerusLogError,
    analyse_verus_log,
    from_verus_log_error,
    parse_expectation,
    parse_verus_log,
)

__all__ = ("ANALYSIS_SCHEMA_VERSION", "verus_analyse_log")

#: Schema version of the JSON object this command writes.
ANALYSIS_SCHEMA_VERSION: typ.Final[int] = 1


def _failure_lines(analysis: VerusAnalysis) -> tuple[str, ...]:
    """Name each failure beside the position it was reported at."""
    return tuple(
        f"failure {item.where}: {item.message}"
        if item.is_located
        else f"failure: {item.message}"
        for item in analysis.failures
    )


def _expectation_line(
    analysis: VerusAnalysis,
    expectation: FailureExpectation | None,
) -> tuple[str, ...]:
    """Report the declared expectation and whether the run met it."""
    if expectation is None:
        return ()
    verdict = "matched" if analysis.expectation_met else "unmatched"
    return (f"expectation {expectation.describe()}: {verdict}",)


def _text_lines(
    analysis: VerusAnalysis,
    expectation: FailureExpectation | None,
) -> tuple[str, ...]:
    """Render the human-readable form, one fact per line."""
    return (
        f"outcome: {analysis.outcome.value}",
        f"run mode: {analysis.run_mode or 'none'}",
        f"reason: {analysis.reason}",
        *_failure_lines(analysis),
        *_expectation_line(analysis, expectation),
    )


def _json_payload(
    analysis: VerusAnalysis,
    expectation: FailureExpectation | None,
) -> dict[str, object]:
    """Render the stable machine-readable form."""
    declared = (
        None
        if expectation is None
        else {
            "path": expectation.path,
            "first_line": expectation.first_line,
            "last_line": expectation.last_line,
            "matched": analysis.expectation_met,
        }
    )
    return {
        "schema_version": ANALYSIS_SCHEMA_VERSION,
        "outcome": analysis.outcome.value,
        "run_mode": analysis.run_mode,
        "passed": analysis.passed,
        "reason": analysis.reason,
        "failures": [
            {
                "message": item.message,
                "path": item.path,
                "line": item.line,
                "column": item.column,
            }
            for item in analysis.failures
        ],
        "expectation": declared,
    }


def verus_analyse_log(
    *,
    log: Path,
    expect_failure: str | None = None,
    require_full: bool = False,
    output_format: typ.Annotated[
        typ.Literal["text", "json"],
        Parameter(name="--format"),
    ] = "text",
) -> int:
    """Analyse a captured Verus log.

    Parameters
    ----------
    log : Path
        Complete captured output of one Verus invocation, with both streams.
    expect_failure : str | None
        ``path[:start[-end]]`` where a negative-control run was required to
        fail.  Exactly one located failure must land inside it.
    require_full : bool
        Whether to fail unless the run verified the whole crate rather than a
        selection made with ``--verify-*``.
    output_format : Literal["text", "json"]
        Output form, defaulting to text.

    Returns
    -------
    int
        ``0`` when the analysis passed and any full-run requirement holds,
        ``2`` when it completed but did not pass, and ``1`` when the log could
        not be read or the expectation is malformed.

    Notes
    -----
    A log that never reached a verification result completes as a
    ``tool-error`` analysis and exits ``2``; only an unreadable file or a
    malformed expectation is rejected before analysis, exiting ``1``.
    """
    try:
        text = read_input_text(log, "log")
        expectation = (
            parse_expectation(expect_failure) if expect_failure is not None else None
        )
    except (InputError, ValueError) as error:
        print_lines((str(error),), stream=sys.stderr)
        return 1
    try:
        analysis = analyse_verus_log(parse_verus_log(text), expectation=expectation)
    except VerusLogError as error:
        analysis = from_verus_log_error(error)
    if output_format == "json":
        lines = (json.dumps(_json_payload(analysis, expectation)),)
    else:
        lines = _text_lines(analysis, expectation)
    print_lines(lines)
    full_run = analysis.run_mode == FULL_RUN
    full_holds = not require_full or full_run
    return 0 if analysis.passed and full_holds else 2
