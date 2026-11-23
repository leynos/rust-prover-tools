"""Adapt a captured Kani log and its manifest to the command line.

The command is deliberately separate from ``kani exec`` and ``kani run``:
analysis reads a transcript, while those commands produce one.  Keeping them
apart lets the analysis run anywhere a log exists, without a Kani installation
or a proof directory.

Only the two inputs are bound to the environment; inherited ``INPUT_``
configuration is disabled, because a stray variable must not silently change
which log is analysed.
"""

from __future__ import annotations

import json
import sys
import typing as typ
from pathlib import Path  # ruff: ignore[typing-only-standard-library-import] - Cyclopts evaluates annotations

from cyclopts import Parameter

from rust_prover_tools.cli_support import InputError, read_input_text
from rust_prover_tools.exit_status import print_lines
from rust_prover_tools.kani_log import (
    FULL_PROFILE,
    KaniAnalysis,
    KaniLogError,
    KaniManifestError,
    Manifest,
    analyse_kani_log,
    from_kani_log_error,
    parse_kani_log,
    parse_manifest,
)

__all__ = ("ANALYSIS_SCHEMA_VERSION", "kani_analyse_log")

#: Schema version of the JSON object this command writes.
ANALYSIS_SCHEMA_VERSION: typ.Final[int] = 1


def _declared_covers(manifest: Manifest) -> tuple[tuple[str, str], ...]:
    """Pair each declared cover with the claim that declares it, in order."""
    return tuple(
        (claim.name, cover) for claim in manifest.claims for cover in claim.covers
    )


def _cover_lines(
    manifest: Manifest,
    analysis: KaniAnalysis,
) -> tuple[str, ...]:
    """Name each declared cover beside the status the log reported.

    A tool error leaves no statuses, so only the covers actually reached are
    named rather than inventing a verdict for the rest.
    """
    return tuple(
        f"cover {harness}: {cover}: {status}"
        for (harness, cover), status in zip(
            _declared_covers(manifest),
            analysis.witness_statuses,
            strict=False,
        )
    )


def _control_lines(
    manifest: Manifest,
    analysis: KaniAnalysis,
) -> tuple[str, ...]:
    """Name each declared control beside its disposition."""
    return tuple(
        f"control {control.name}: {status}"
        for control, status in zip(
            manifest.controls,
            analysis.control_statuses,
            strict=False,
        )
    )


def _text_lines(manifest: Manifest, analysis: KaniAnalysis) -> tuple[str, ...]:
    """Render the human-readable form, one fact per line."""
    return (
        f"outcome: {analysis.outcome.value}",
        f"reason: {analysis.reason}",
        f"run mode: {analysis.run_mode}",
        *_cover_lines(manifest, analysis),
        *_control_lines(manifest, analysis),
    )


def _json_payload(
    manifest: Manifest,
    analysis: KaniAnalysis,
) -> dict[str, object]:
    """Render the stable machine-readable form."""
    covers = [
        {"harness": harness, "cover": cover, "status": status}
        for (harness, cover), status in zip(
            _declared_covers(manifest),
            analysis.witness_statuses,
            strict=False,
        )
    ]
    controls = [
        {"harness": control.name, "status": status}
        for control, status in zip(
            manifest.controls,
            analysis.control_statuses,
            strict=False,
        )
    ]
    return {
        "schema_version": ANALYSIS_SCHEMA_VERSION,
        "outcome": analysis.outcome.value,
        "reason": analysis.reason,
        "run_mode": analysis.run_mode,
        "passed": analysis.passed,
        "covers": covers,
        "controls": controls,
    }


# The public command needs one parameter per documented flag.
# pylint: disable-next=too-many-arguments
def kani_analyse_log(  # ruff: ignore[too-many-arguments] - one keyword per CLI flag
    *,
    log: Path,
    manifest: Path,
    harness: typ.Annotated[
        list[str] | None,
        Parameter(allow_repeating=True, negative=()),
    ] = None,
    require_full_profile: bool = False,
    output_format: typ.Annotated[
        typ.Literal["text", "json"],
        Parameter(name="--format"),
    ] = "text",
) -> int:
    """Analyse a captured Kani log against a harness manifest.

    Parameters
    ----------
    log : Path
        Complete captured output of one Kani invocation.
    manifest : Path
        Harness manifest declaring claims, controls and covers.
    harness : list[str] | None
        Harness names to select from the manifest; defaults to all of them.
    require_full_profile : bool
        Whether to fail unless the run covered the whole selected manifest with
        unwinding checks enabled.
    output_format : Literal["text", "json"]
        Output form, defaulting to text.

    Returns
    -------
    int
        ``0`` when the analysis passed and any profile requirement holds,
        ``2`` when it completed but did not pass or passed while
        ``require_full_profile`` rejected its run mode, and ``1`` when an
        input could not be read or validated.  The JSON ``passed`` field
        reports the analysis alone, so a consumer requiring the full profile
        must also read ``run_mode`` or the exit status.

    Notes
    -----
    A truncated or otherwise structurally invalid log completes as a
    ``tool-error`` analysis and exits ``2``.  An unreadable file or an invalid
    manifest is rejected before analysis and exits ``1``.
    """
    try:
        log_text = read_input_text(log, "log")
        manifest_text = read_input_text(manifest, "manifest")
        parsed_manifest = parse_manifest(manifest_text)
        # The declaration outlives the selection: a log from a wider run can
        # report a harness the caller did not select, and the analysis skips
        # it rather than calling it undeclared.
        declared = parsed_manifest.names()
        if harness:
            parsed_manifest = parsed_manifest.select(tuple(harness))
        try:
            analysis = analyse_kani_log(
                parse_kani_log(log_text),
                parsed_manifest,
                declared=declared,
            )
        except KaniLogError as error:
            analysis = from_kani_log_error(error)
    except (KaniManifestError, InputError) as error:
        print_lines((str(error),), stream=sys.stderr)
        return 1
    if output_format == "json":
        lines = (json.dumps(_json_payload(parsed_manifest, analysis)),)
    else:
        lines = _text_lines(parsed_manifest, analysis)
    print_lines(lines)
    profile_holds = not require_full_profile or analysis.run_mode == FULL_PROFILE
    return 0 if analysis.passed and profile_holds else 2
