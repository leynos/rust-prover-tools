"""Prove the Kani analysis command honours its output and exit contract."""

from __future__ import annotations

import json
import typing as typ
from pathlib import Path

from rust_prover_tools.kani_log import SUPPORTED_KANI_VERSIONS
from rust_prover_tools.kani_log_cli import kani_analyse_log

if typ.TYPE_CHECKING:
    import pytest

FIXTURES = (
    Path(__file__).resolve().parents[2]
    / "tests"
    / "fixtures"
    / "kani_logs"
    / SUPPORTED_KANI_VERSIONS[0]
)
MANIFEST = FIXTURES / "corpus-manifest.json"


def run(
    log: str,
    *,
    manifest: Path = MANIFEST,
    harness: list[str] | None = None,
    require_full_profile: bool = False,
    output_format: typ.Literal["text", "json"] = "text",
) -> int:
    """Run the command with every optional flag explicit."""
    return kani_analyse_log(
        log=FIXTURES / f"{log}.log",
        manifest=manifest,
        harness=harness,
        require_full_profile=require_full_profile,
        output_format=output_format,
    )


def test_a_verified_log_passes_and_prints_its_cover(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Report the outcome, run mode and cover in text."""
    status = run("verified", harness=["harnesses::verified"])
    captured = capsys.readouterr()
    assert status == 0, "Report the outcome, run mode and cover in text."
    assert "outcome: verified\n" in captured.out, (
        "Report the outcome, run mode and cover in text."
    )
    assert "run mode: full-profile\n" in captured.out, (
        "Report the outcome, run mode and cover in text."
    )
    assert (
        "cover harnesses::verified: the boundary below saturation is "
        "reachable: satisfied\n"
    ) in captured.out, "Report the outcome, run mode and cover in text."


def test_a_counterexample_completes_without_passing(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Use status 2 for a completed analysis that did not pass."""
    status = run("counterexample", harness=["harnesses::counterexample"])
    assert status == 2, "Use status 2 for a completed analysis that did not pass."
    assert "outcome: counterexample\n" in capsys.readouterr().out, (
        "Use status 2 for a completed analysis that did not pass."
    )


def test_a_truncated_log_is_a_tool_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A structurally invalid log completes as a tool error, not an input error."""
    status = run("truncated")
    assert status == 2, (
        "A structurally invalid log completes as a tool error, not an input error."
    )
    assert "outcome: tool-error\n" in capsys.readouterr().out, (
        "A structurally invalid log completes as a tool error, not an input error."
    )


def test_requiring_a_full_profile_fails_a_focused_run(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The profile requirement turns a passing focused run into status 2.

    A log whose block never echoed its solver command cannot show whether
    unwinding checks ran, so the run stays focused even though it passed.
    """
    log = tmp_path / "verified.log"
    solver_echo = "[Kani] Running: `cbmc "
    lines = (FIXTURES / "verified.log").read_text(encoding="utf-8").splitlines()
    log.write_text(
        "\n".join(line for line in lines if not line.startswith(solver_echo)),
        encoding="utf-8",
    )
    without_requirement = kani_analyse_log(
        log=log,
        manifest=MANIFEST,
        harness=["harnesses::verified"],
        require_full_profile=False,
        output_format="text",
    )
    capsys.readouterr()
    with_requirement = kani_analyse_log(
        log=log,
        manifest=MANIFEST,
        harness=["harnesses::verified"],
        require_full_profile=True,
        output_format="text",
    )
    captured = capsys.readouterr()
    assert without_requirement == 0, (
        "The profile requirement turns a passing focused run into status 2."
    )
    assert with_requirement == 2, (
        "The profile requirement turns a passing focused run into status 2."
    )
    assert captured.out == (
        "outcome: verified\n"
        "reason: decided from the parsed log\n"
        "run mode: focused\n"
        "cover harnesses::verified: the boundary below saturation is "
        "reachable: satisfied\n"
    ), "The profile requirement turns a passing focused run into status 2."


def test_an_invalid_manifest_is_an_input_error(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Reject a manifest that is not JSON with status 1."""
    manifest = tmp_path / "manifest.json"
    manifest.write_text("{not json", encoding="utf-8")
    status = run("verified", manifest=manifest)
    captured = capsys.readouterr()
    assert status == 1, "Reject a manifest that is not JSON with status 1."
    assert "Kani harness manifest" in captured.err, (
        "Reject a manifest that is not JSON with status 1."
    )


def test_a_deeply_nested_manifest_is_an_input_error(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Reject a deeply nested manifest with status 1.

    A decoder with a recursive limit raises :class:`RecursionError`; a newer
    decoder that parses iteratively returns an array the parser refuses.  Both
    are unreadable input, so the command exits 1 either way.
    """
    manifest = tmp_path / "manifest.json"
    manifest.write_text("[" * 100_000 + "]" * 100_000, encoding="utf-8")
    status = run("verified", manifest=manifest)
    captured = capsys.readouterr()
    assert status == 1, "Reject a deeply nested manifest with status 1."
    assert "nested too deeply" in captured.err or "must be a JSON object" in (
        captured.err
    ), "Reject a deeply nested manifest with status 1."


def test_an_unreadable_log_is_an_input_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Reject a missing log with status 1 and a diagnostic."""
    status = kani_analyse_log(
        log=FIXTURES / "missing.log",
        manifest=MANIFEST,
        harness=None,
        require_full_profile=False,
        output_format="text",
    )
    captured = capsys.readouterr()
    assert status == 1, "Reject a missing log with status 1 and a diagnostic."
    assert "cannot read log" in captured.err, (
        "Reject a missing log with status 1 and a diagnostic."
    )


def test_json_output_is_a_stable_object(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Emit the documented object with one record per cover and control."""
    status = run(
        "default_unwind",
        harness=[
            "harnesses::verified",
            "harnesses::seeded_wrapping_mutant",
        ],
        output_format="json",
    )
    payload = json.loads(capsys.readouterr().out)
    assert status == 0, (
        "Emit the documented object with one record per cover and control."
    )
    assert list(payload) == [
        "schema_version",
        "outcome",
        "reason",
        "run_mode",
        "passed",
        "covers",
        "controls",
    ], "Emit the documented object with one record per cover and control."
    assert payload["schema_version"] == 1, (
        "Emit the documented object with one record per cover and control."
    )
    assert payload["outcome"] == "verified", (
        "Emit the documented object with one record per cover and control."
    )
    assert payload["run_mode"] == "full-profile", (
        "Emit the documented object with one record per cover and control."
    )
    assert payload["passed"] is True, (
        "Emit the documented object with one record per cover and control."
    )
    assert payload["covers"] == [
        {
            "harness": "harnesses::verified",
            "cover": "the boundary below saturation is reachable",
            "status": "satisfied",
        },
    ], "Emit the documented object with one record per cover and control."
    assert payload["controls"] == [
        {
            "harness": "harnesses::seeded_wrapping_mutant",
            "status": "rejected-as-intended",
        },
    ], "Emit the documented object with one record per cover and control."
