"""Behavioural steps for Verus log analysis through the public CLI."""

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

from rust_prover_tools.verus_log import SUPPORTED_VERUS_VERSIONS

scenarios("../features/verus_log.feature")

FIXTURES: typ.Final[Path] = (
    Path(__file__).resolve().parents[2]
    / "tests"
    / "fixtures"
    / "verus_logs"
    / SUPPORTED_VERUS_VERSIONS[0]
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
def declared_region() -> list[str | None]:
    """Collect the failure region a scenario declares."""
    return [None]


@pytest.fixture
def requires_full_run() -> list[bool]:
    """Collect whether a scenario demands a full run."""
    return [False]


@pytest.fixture
def named_release() -> list[str | None]:
    """Collect the release a scenario names."""
    return [None]


@given(parsers.parse('the captured Verus log "{name}"'), target_fixture="log_path")
def captured_log(name: str) -> Path:
    """Name one committed corpus log; an absent one exercises the read failure."""
    return FIXTURES / f"{name}.log"


@given(
    "a log whose clean summary sits beside a failure",
    target_fixture="log_path",
)
def contradictory_log(tmp_path: Path) -> Path:
    """Write a log that reports a failure while counting none."""
    path = tmp_path / "contradictory.log"
    path.write_text(
        "\n".join(
            (
                "error: assertion failed",
                "  --> proofs/lib.rs:3:5",
                "verification results:: 1 verified, 0 errors",
            ),
        ),
        encoding="utf-8",
    )
    return path


@given(parsers.parse('the declared failure region "{region}"'))
def declared_failure_region(declared_region: list[str | None], region: str) -> None:
    """Record where the negative control was meant to fail."""
    declared_region[0] = region


@given("a full run is required")
def full_run_required(requires_full_run: list[bool]) -> None:
    """Demand that the analysed run verified the whole crate."""
    requires_full_run[0] = True


@given(parsers.parse('the Verus release "{release}" is named'))
def named_verus_release(named_release: list[str | None], release: str) -> None:
    """Record the release the analysed log was captured from.

    ``--repo-root`` points at the corpus directory, which holds no
    ``tools/verus/VERSION`` pin, so the scenario's flag is the only source of
    a release and an unqualified one is still refused.
    """
    named_release[0] = release


@dc.dataclass(frozen=True, slots=True)
class DeclaredOptions:
    """The options one scenario declared for its analysis."""

    region: str | None
    requires_full_run: bool
    release: str | None


def declared_options(
    declared_region: list[str | None],
    requires_full_run: list[bool],
    named_release: list[str | None],
) -> DeclaredOptions:
    """Collect the fixtures into the options the helper reads."""
    return DeclaredOptions(
        region=declared_region[0],
        requires_full_run=requires_full_run[0],
        release=named_release[0],
    )


def run_analysis(
    log_path: Path,
    options: DeclaredOptions,
    *,
    output_json: bool,
) -> CliRun:
    """Run `verus analyse-log` with one flag per declared option."""
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "rust_prover_tools.cli",
            "verus",
            "analyse-log",
            "--log",
            str(log_path),
            "--repo-root",
            str(FIXTURES),
            *(["--verus-release", options.release] if options.release else []),
            *(["--expect-failure", options.region] if options.region else []),
            *(["--require-full"] if options.requires_full_run else []),
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


@when("the user analyses the Verus log", target_fixture="cli_run")
def user_analyses_log(
    log_path: Path,
    declared_region: list[str | None],
    requires_full_run: list[bool],
    named_release: list[str | None],
) -> CliRun:
    """Analyse one log as text."""
    return run_analysis(
        log_path,
        declared_options(declared_region, requires_full_run, named_release),
        output_json=False,
    )


@when("the user analyses the Verus log in JSON", target_fixture="cli_run")
def user_analyses_log_in_json(
    log_path: Path,
    declared_region: list[str | None],
    requires_full_run: list[bool],
    named_release: list[str | None],
) -> CliRun:
    """Analyse one log as a JSON object."""
    return run_analysis(
        log_path,
        declared_options(declared_region, requires_full_run, named_release),
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


@then("the command completes without passing")
def command_completes_without_passing(cli_run: CliRun) -> None:
    """Assert the documented completed-but-not-passing status."""
    assert cli_run.returncode == 2, cli_run


@then(parsers.parse('stdout reports the Verus outcome "{outcome}"'))
def stdout_reports_outcome(cli_run: CliRun, outcome: str) -> None:
    """Assert the outcome line is present."""
    assert f"outcome: {outcome}\n" in cli_run.stdout, cli_run.stdout


@then(parsers.parse('stdout reports the run mode "{run_mode}"'))
def stdout_reports_run_mode(cli_run: CliRun, run_mode: str) -> None:
    """Assert the run-mode line is present."""
    assert f"run mode: {run_mode}\n" in cli_run.stdout, cli_run.stdout


@then(parsers.parse('stdout names the failure "{where}"'))
def stdout_names_failure(cli_run: CliRun, where: str) -> None:
    """Assert the failure line carries the reported position."""
    assert f"failure {where}: " in cli_run.stdout, cli_run.stdout


@then("stdout reports a structural reason")
def stdout_reports_structural_reason(cli_run: CliRun) -> None:
    """Assert a tool error carries the parser's reason."""
    assert "reason: the log records no verification errors" in cli_run.stdout, (
        cli_run.stdout
    )


@then("stderr names the unreadable log")
def stderr_names_unreadable_log(cli_run: CliRun) -> None:
    """Assert the diagnostic names the file and why it could not be read."""
    assert "cannot read log" in cli_run.stderr, cli_run.stderr


@then("stdout reports the declared expectation as matched")
def stdout_reports_matched_expectation(cli_run: CliRun) -> None:
    """Assert the expectation line records a match."""
    assert ": matched\n" in cli_run.stdout, cli_run.stdout


@then("stdout reports the declared expectation as unmatched")
def stdout_reports_unmatched_expectation(cli_run: CliRun) -> None:
    """Assert the expectation line records a miss."""
    assert ": unmatched\n" in cli_run.stdout, cli_run.stdout


@then(
    parsers.parse(
        'stdout is a stable Verus JSON analysis with outcome "{outcome}" '
        'and run mode "{run_mode}"',
    ),
)
def stdout_is_json_analysis(cli_run: CliRun, outcome: str, run_mode: str) -> None:
    """Parse the JSON object and check its stable fields."""
    payload = json.loads(cli_run.stdout)
    assert payload["schema_version"] == 1
    assert payload["outcome"] == outcome
    assert payload["run_mode"] == run_mode
    assert payload["passed"] is False
    assert [failure["line"] for failure in payload["failures"]] == [8, 15]


@then(parsers.parse('stdout reports the Verus release "{release}"'))
def stdout_reports_release(cli_run: CliRun, release: str) -> None:
    """Assert the report names the release the log was checked against."""
    assert f"verus release: {release}\n" in cli_run.stdout, cli_run.stdout


@then("stderr reports the release as unsupported")
def stderr_reports_unsupported_release(cli_run: CliRun) -> None:
    """Assert the refusal names the release and the qualified set."""
    assert "is not one this parser is qualified against" in cli_run.stderr, (
        cli_run.stderr
    )
    assert "0.1999.01.01.deadbeef" in cli_run.stderr, cli_run.stderr
