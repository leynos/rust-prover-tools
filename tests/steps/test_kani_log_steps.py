"""Behavioural steps for Kani log analysis through the public CLI."""

from __future__ import annotations

import dataclasses as dc
import json
import os
import subprocess
import sys
import typing as typ
from pathlib import Path

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

from rust_prover_tools.kani_log import SUPPORTED_KANI_VERSIONS

scenarios("../features/kani_log.feature")

FIXTURES: typ.Final[Path] = (
    Path(__file__).resolve().parents[2]
    / "tests"
    / "fixtures"
    / "kani_logs"
    / SUPPORTED_KANI_VERSIONS[0]
)


def sanitized_subprocess_env() -> dict[str, str]:
    """Return a subprocess environment without inherited input bindings."""
    return {
        key: value for key, value in os.environ.items() if not key.startswith("INPUT_")
    }


@dc.dataclass(frozen=True, slots=True)
class CliRun:
    """One captured command-line run."""

    stdout: str
    stderr: str
    returncode: int


@pytest.fixture
def selected_harnesses() -> list[str]:
    """Collect the harness names a scenario selects, in order."""
    return []


@pytest.fixture
def manifest_path() -> Path:
    """Return the corpus manifest a scenario analyses against."""
    return FIXTURES / "corpus-manifest.json"


@given(parsers.parse('the captured Kani log "{name}"'), target_fixture="log_path")
def captured_log(name: str) -> Path:
    """Name one committed corpus log."""
    return FIXTURES / f"{name}.log"


@given("the corpus harness manifest")
def corpus_manifest() -> None:
    """Use the manifest covering every corpus harness."""


@given(parsers.parse('the selected harness "{name}"'))
def selected_harness(selected_harnesses: list[str], name: str) -> None:
    """Add one harness selection to the command."""
    selected_harnesses.append(name)


@given(
    "an invalid harness manifest",
    target_fixture="manifest_path",
)
def invalid_manifest(tmp_path: Path) -> Path:
    """Write a manifest document that is not JSON."""
    path = tmp_path / "invalid-manifest.json"
    path.write_text("{not a manifest", encoding="utf-8")
    return path


def run_analysis(
    log_path: Path,
    manifest_path: Path,
    selected_harnesses: list[str],
    *,
    output_json: bool,
) -> CliRun:
    """Run `kani analyse-log` with one flag per selection."""
    selection = [
        argument
        for harness in selected_harnesses
        for argument in ("--harness", harness)
    ]
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "rust_prover_tools.cli",
            "kani",
            "analyse-log",
            "--log",
            str(log_path),
            "--manifest",
            str(manifest_path),
            *selection,
            *(["--format", "json"] if output_json else []),
        ],
        check=False,
        capture_output=True,
        text=True,
        env=sanitized_subprocess_env(),
    )
    return CliRun(
        completed.stdout,
        completed.stderr,
        completed.returncode,
    )


@when("the user analyses the Kani log", target_fixture="cli_run")
def user_analyses_log(
    log_path: Path,
    manifest_path: Path,
    selected_harnesses: list[str],
) -> CliRun:
    """Analyse one log as text."""
    return run_analysis(
        log_path,
        manifest_path,
        selected_harnesses,
        output_json=False,
    )


@when("the user analyses the Kani log in JSON", target_fixture="cli_run")
def user_analyses_log_in_json(
    log_path: Path,
    manifest_path: Path,
    selected_harnesses: list[str],
) -> CliRun:
    """Analyse one log as a JSON object."""
    return run_analysis(
        log_path,
        manifest_path,
        selected_harnesses,
        output_json=True,
    )


@then("the command succeeds")
def command_succeeds(cli_run: CliRun) -> None:
    """Assert that the analysis passed."""
    assert cli_run.returncode == 0, cli_run


@then("the command fails")
def command_fails(cli_run: CliRun) -> None:
    """Assert that the command rejected its input."""
    assert cli_run.returncode == 1, cli_run


@then("the command completes without a passing analysis")
def command_completes_without_passing(cli_run: CliRun) -> None:
    """Assert the documented completed-but-not-passing status."""
    assert cli_run.returncode == 2, cli_run


@then(parsers.parse('stdout reports outcome "{outcome}"'))
def stdout_reports_outcome(cli_run: CliRun, outcome: str) -> None:
    """Assert the outcome line is present."""
    assert f"outcome: {outcome}\n" in cli_run.stdout, cli_run.stdout


@then(
    parsers.parse(
        'stdout reports cover "{cover}" with status "{status}"',
    ),
)
def stdout_reports_cover(cli_run: CliRun, cover: str, status: str) -> None:
    """Assert the cover line names its status."""
    assert f": {cover}: {status}\n" in cli_run.stdout, cli_run.stdout


@then("stdout reports the structural reason")
def stdout_reports_structural_reason(cli_run: CliRun) -> None:
    """Assert a tool error carries the parser's reason."""
    assert "reason: the log is truncated" in cli_run.stdout, cli_run.stdout


@then("stderr names the invalid manifest")
def stderr_names_invalid_manifest(cli_run: CliRun) -> None:
    """Assert the diagnostic names the manifest and why it was refused."""
    assert "Kani harness manifest" in cli_run.stderr, cli_run.stderr
    assert "not valid JSON" in cli_run.stderr, cli_run.stderr


@then(
    parsers.parse(
        'stdout is a stable JSON analysis with outcome "{outcome}" '
        'and run mode "{run_mode}"',
    ),
)
def stdout_is_json_analysis(
    cli_run: CliRun,
    outcome: str,
    run_mode: str,
) -> None:
    """Parse the JSON object and check its stable fields."""
    payload = json.loads(cli_run.stdout)
    assert payload["schema_version"] == 1
    assert payload["outcome"] == outcome
    assert payload["run_mode"] == run_mode
    assert payload["passed"] is True
    assert [cover["status"] for cover in payload["covers"]] == ["satisfied"]
    assert [control["status"] for control in payload["controls"]] == [
        "rejected-as-intended",
    ]
