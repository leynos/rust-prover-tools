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
from pathlib import Path

from cyclopts import Parameter

from rust_prover_tools.cli_support import InputError, read_input_text
from rust_prover_tools.exit_status import print_lines
from rust_prover_tools.verus import VERUS_VERSION_PATH
from rust_prover_tools.verus_log import (
    FULL_RUN,
    SUPPORTED_VERUS_VERSIONS,
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


def _release_line(release: str | None) -> tuple[str, ...]:
    """Name the qualified release the log was checked against, when known."""
    return () if release is None else (f"verus release: {release}",)


def _text_lines(
    analysis: VerusAnalysis,
    expectation: FailureExpectation | None,
    release: str | None,
) -> tuple[str, ...]:
    """Render the human-readable form, one fact per line."""
    return (
        f"outcome: {analysis.outcome.value}",
        f"run mode: {analysis.run_mode or 'none'}",
        f"reason: {analysis.reason}",
        *_release_line(release),
        *_failure_lines(analysis),
        *_expectation_line(analysis, expectation),
    )


def _json_payload(
    analysis: VerusAnalysis,
    expectation: FailureExpectation | None,
    release: str | None,
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
        "verus_release": release,
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


def _pinned_release(repo_root: Path) -> str | None:
    """Return the release the consumer pinned, or ``None`` when it pinned none.

    A repository that has not installed Verus has no pin, and its log is read
    without one rather than refused: analysing a transcript needs no Verus
    installation, so demanding a pin would make the command unusable exactly
    where it is most convenient.

    Raises
    ------
    ValueError
        If the pin exists but holds no release, because the file's presence
        says the consumer meant to record one.
    """
    version_file = repo_root / VERUS_VERSION_PATH
    if not version_file.exists():
        return None
    pinned = version_file.read_text(encoding="utf-8").strip()
    if not pinned:
        msg = f"{version_file} is empty, so it names no Verus release"
        raise ValueError(msg)
    return pinned


def _resolve_release(
    release: str | None,
    repo_root: Path,
) -> tuple[str | None, str | None]:
    """Return the release to name and the error explaining a refusal.

    A named release is checked as given; without one the consumer's pin is
    read and checked the same way.  Naming the release matters because the
    grammar is qualified per release: a log from one the parser has not been
    captured against may print a form it does not recognize, and reading it
    anyway would report facts the release never printed.

    Returns
    -------
    tuple[str | None, str | None]
        The accepted release, and the diagnostic when none was accepted.
    """
    try:
        selected = release if release is not None else _pinned_release(repo_root)
    except (OSError, UnicodeDecodeError, ValueError) as error:
        return None, str(error)
    if selected is None:
        return None, None
    if selected not in SUPPORTED_VERUS_VERSIONS:
        supported = ", ".join(SUPPORTED_VERUS_VERSIONS)
        return None, (
            f"Verus release {selected} is not one this parser is qualified "
            f"against; supported releases are {supported}"
        )
    return selected, None


# The public command needs one parameter per documented flag.
# pylint: disable-next=too-many-arguments
def verus_analyse_log(  # ruff: ignore[too-many-arguments] - one keyword per CLI flag
    *,
    log: Path,
    expect_failure: str | None = None,
    require_full: bool = False,
    verus_release: typ.Annotated[
        str | None,
        Parameter(name="--verus-release"),
    ] = None,
    repo_root: typ.Annotated[
        Path | None,
        Parameter(name="--repo-root"),
    ] = None,
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
    verus_release : str | None
        The Verus release the log was captured from.  Defaults to the consumer's
        ``tools/verus/VERSION`` pin when one exists; without either, the
        release is left unnamed rather than guessed.
    repo_root : Path | None
        Repository root holding the pin.  Defaults to the working directory.
    output_format : Literal["text", "json"]
        Output form, defaulting to text.

    Returns
    -------
    int
        ``0`` when the analysis passed and any full-run requirement holds,
        ``2`` when it completed but did not pass, and ``1`` when the log could
        not be read, the expectation is malformed, or the release named is not
        one the parser is qualified against.

    Notes
    -----
    A log that never reached a verification result completes as a
    ``tool-error`` analysis and exits ``2``; only an unreadable file, a
    malformed expectation or an unsupported release is rejected before
    analysis, exiting ``1``.
    """
    release, refusal = _resolve_release(
        verus_release,
        (repo_root or Path.cwd()).resolve(),
    )
    if refusal is not None:
        print_lines((refusal,), stream=sys.stderr)
        return 1
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
        lines = (json.dumps(_json_payload(analysis, expectation, release)),)
    else:
        lines = _text_lines(analysis, expectation, release)
    print_lines(lines)
    full_run = analysis.run_mode == FULL_RUN
    full_holds = not require_full or full_run
    return 0 if analysis.passed and full_holds else 2
