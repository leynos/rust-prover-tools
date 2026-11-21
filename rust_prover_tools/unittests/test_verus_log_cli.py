"""Prove the Verus analysis command honours its output and exit contract."""

from __future__ import annotations

import json
import typing as typ
from pathlib import Path

from rust_prover_tools.verus_log import SUPPORTED_VERUS_VERSIONS
from rust_prover_tools.verus_log_cli import verus_analyse_log

if typ.TYPE_CHECKING:
    import pytest

FIXTURES: typ.Final[Path] = (
    Path(__file__).resolve().parents[2]
    / "tests"
    / "fixtures"
    / "verus_logs"
    / SUPPORTED_VERUS_VERSIONS[0]
)


def run(
    log: str,
    *,
    expect_failure: str | None = None,
    require_full: bool = False,
    verus_release: str | None = None,
    repo_root: Path | None = None,
    output_format: typ.Literal["text", "json"] = "text",
) -> int:
    """Run the command with every optional flag explicit.

    ``repo_root`` defaults to a directory with no pin, so a test states which
    release it means instead of inheriting whatever the working tree pins.
    """
    return verus_analyse_log(
        log=FIXTURES / f"{log}.log",
        expect_failure=expect_failure,
        require_full=require_full,
        verus_release=verus_release,
        repo_root=repo_root if repo_root is not None else FIXTURES,
        output_format=output_format,
    )


def test_a_verified_log_passes_and_names_its_run_mode(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Report the outcome, run mode and reason in text."""
    status = run("verified")
    captured = capsys.readouterr()

    assert status == 0
    assert "outcome: verified\n" in captured.out
    assert "run mode: full\n" in captured.out
    assert "reason: the log reports 3 verified and no errors\n" in captured.out


def test_a_failing_log_completes_without_passing(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Use status 2 for a completed analysis that did not pass."""
    status = run("postcondition")
    captured = capsys.readouterr()

    assert status == 2
    assert "outcome: errors\n" in captured.out
    assert "failure postcondition.rs:8:13: postcondition not satisfied\n" in (
        captured.out
    )


def test_a_diagnostic_without_a_span_names_no_position(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A usage error prints as a bare failure rather than a torn path."""
    status = run("usage_error")
    captured = capsys.readouterr()

    assert status == 2
    assert "outcome: tool-error\n" in captured.out
    assert "run mode: none\n" in captured.out
    assert "failure: `--verify-function` option requires" in captured.out


def test_an_invalid_log_file_is_an_input_error(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An unreadable log is reported before analysis, with status 1."""
    missing = tmp_path / "absent.log"

    status = verus_analyse_log(
        log=missing,
        verus_release=SUPPORTED_VERUS_VERSIONS[0],
        repo_root=tmp_path,
    )

    assert status == 1
    assert "cannot read log" in capsys.readouterr().err


def test_a_malformed_expectation_is_an_input_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A region that cannot be parsed is rejected before analysis.

    The command reads the log first and then parses the expectation, so a
    malformed region fails before any analysis but after the read.
    """
    status = run("postcondition", expect_failure="postcondition.rs:4-1")
    captured = capsys.readouterr()

    assert status == 1
    assert "failure expectation" in captured.err
    assert captured.out == ""


def test_a_negative_control_passes_inside_its_declared_region(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A failing run passes when the failure landed where it was meant to."""
    status = run("postcondition", expect_failure="postcondition.rs:8")
    captured = capsys.readouterr()

    assert status == 0
    assert "outcome: errors\n" in captured.out
    assert "expectation postcondition.rs:8: matched\n" in captured.out


def test_a_negative_control_failing_elsewhere_completes_without_passing(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A failure outside the declared region leaves the control unmatched."""
    status = run("postcondition", expect_failure="postcondition.rs:20")
    captured = capsys.readouterr()

    assert status == 2
    assert "expectation postcondition.rs:20: unmatched\n" in captured.out


def test_requiring_a_full_run_fails_a_selected_run(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A selected run proves something, but not the whole crate."""
    status = run("selected_function", require_full=True)
    captured = capsys.readouterr()

    assert status == 2
    assert "outcome: verified\n" in captured.out
    assert "run mode: partial\n" in captured.out


def test_a_full_run_without_the_requirement_passes(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The run-mode requirement is opt-in."""
    status = run("selected_function")
    capsys.readouterr()

    assert status == 0


def test_the_json_form_is_stable_and_machine_readable(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The JSON object carries the schema, the decision and each failure."""
    status = run(
        "two_errors",
        verus_release=SUPPORTED_VERUS_VERSIONS[0],
        output_format="json",
    )
    payload = json.loads(capsys.readouterr().out)

    assert status == 2
    assert payload == {
        "schema_version": 1,
        "outcome": "errors",
        "run_mode": "full",
        "passed": False,
        "reason": "the log reports 2 failing functions",
        "verus_release": SUPPORTED_VERUS_VERSIONS[0],
        "failures": [
            {
                "message": "postcondition not satisfied",
                "path": "two_errors.rs",
                "line": 8,
                "column": 13,
            },
            {
                "message": "postcondition not satisfied",
                "path": "two_errors.rs",
                "line": 15,
                "column": 13,
            },
        ],
        "expectation": None,
    }


def test_the_json_form_records_the_declared_expectation(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A declared region is reported beside whether it was met."""
    status = run(
        "postcondition",
        expect_failure="postcondition.rs:5-12",
        output_format="json",
    )
    payload = json.loads(capsys.readouterr().out)

    assert status == 0
    assert payload["expectation"] == {
        "path": "postcondition.rs",
        "first_line": 5,
        "last_line": 12,
        "matched": True,
    }


def test_a_single_line_expectation_reads_as_it_was_written(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A one-line declaration is recorded as ``path:line``, not as a range."""
    status = run(
        "postcondition",
        expect_failure="postcondition.rs:8",
        output_format="json",
    )
    payload = json.loads(capsys.readouterr().out)

    assert status == 0, "The failure landed on the declared line"
    assert payload["expectation"] == {
        "path": "postcondition.rs",
        "first_line": 8,
        "last_line": None,
        "matched": True,
    }, "A single line keeps no end, so it is not echoed back as ``8-8``"


def test_a_structural_error_completes_as_a_tool_error(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A self-contradicting log is a tool error, not an input error."""
    log = tmp_path / "contradictory.log"
    log.write_text(
        "\n".join(
            (
                "error: assertion failed",
                "  --> proofs/lib.rs:3:5",
                "verification results:: 1 verified, 0 errors",
            ),
        ),
        encoding="utf-8",
    )

    status = verus_analyse_log(
        log=log,
        verus_release=SUPPORTED_VERUS_VERSIONS[0],
        repo_root=tmp_path,
    )
    captured = capsys.readouterr()

    assert status == 2
    assert "outcome: tool-error\n" in captured.out
    assert "reason: the log records no verification errors" in captured.out


def test_a_named_supported_release_is_reported(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A release the parser is qualified against is named beside the outcome."""
    release = SUPPORTED_VERUS_VERSIONS[0]

    status = run("verified", verus_release=release)
    captured = capsys.readouterr()

    assert status == 0
    assert f"verus release: {release}\n" in captured.out


def test_an_unsupported_release_is_rejected_before_analysis(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A release with no captured corpus is refused with status 1."""
    status = run("verified", verus_release="0.1999.01.01.deadbeef")
    captured = capsys.readouterr()

    assert status == 1
    assert "0.1999.01.01.deadbeef" in captured.err
    assert "not one this parser is qualified against" in captured.err
    assert SUPPORTED_VERUS_VERSIONS[0] in captured.err
    assert captured.out == ""


def test_the_consumer_pin_supplies_the_release(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Without a flag, the release comes from ``tools/verus/VERSION``."""
    release = SUPPORTED_VERUS_VERSIONS[0]
    version_file = tmp_path / "tools" / "verus" / "VERSION"
    version_file.parent.mkdir(parents=True)
    version_file.write_text(f"{release}\n", encoding="utf-8")

    status = run("verified", repo_root=tmp_path)
    captured = capsys.readouterr()

    assert status == 0
    assert f"verus release: {release}\n" in captured.out


def test_an_unsupported_pin_is_rejected_before_analysis(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A pin naming an unqualified release is refused, not silently ignored."""
    version_file = tmp_path / "tools" / "verus" / "VERSION"
    version_file.parent.mkdir(parents=True)
    version_file.write_text("0.1999.01.01.deadbeef\n", encoding="utf-8")

    status = run("verified", repo_root=tmp_path)
    captured = capsys.readouterr()

    assert status == 1
    assert "0.1999.01.01.deadbeef" in captured.err
    assert captured.out == ""


def test_an_empty_pin_is_rejected(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A pin that exists but names nothing is an error, not an absent pin."""
    version_file = tmp_path / "tools" / "verus" / "VERSION"
    version_file.parent.mkdir(parents=True)
    version_file.write_text("\n", encoding="utf-8")

    status = run("verified", repo_root=tmp_path)
    captured = capsys.readouterr()

    assert status == 1
    assert "is empty, so it names no Verus release" in captured.err
    assert captured.out == ""


def test_without_a_pin_or_a_flag_the_release_stays_unnamed(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Analysis still reads a log where nothing records the release.

    The corpus directories have no pin, which is the shape of a log captured
    on a machine with no Verus installation: the facts are still readable, and
    the report says plainly that no release was checked.
    """
    status = run("verified", output_format="json")
    payload = json.loads(capsys.readouterr().out)

    assert status == 0
    assert payload["verus_release"] is None


def test_the_json_form_reports_the_analysis_before_the_full_run_gate(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``passed`` describes the analysis; ``--require-full`` decides the exit.

    A consumer that requires the whole crate must read ``run_mode`` or the
    exit status, because the payload is written before the gate is applied.
    """
    status = run("selected_function", require_full=True, output_format="json")
    payload = json.loads(capsys.readouterr().out)

    assert status == 2
    assert payload["run_mode"] == "partial"
    assert payload["passed"] is True
