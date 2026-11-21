"""Behavioural steps for the trust audit through the public CLI."""

from __future__ import annotations

import dataclasses as dc
import json
import os
import subprocess
import sys
import typing as typ

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

if typ.TYPE_CHECKING:
    from pathlib import Path

scenarios("../features/trust_audit.feature")


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
def repo_root(tmp_path: Path) -> Path:
    """Provide an empty repository root for a scenario to fill."""
    return tmp_path


@pytest.fixture
def selected_provers() -> list[str]:
    """Collect the provers a scenario selects."""
    return []


@pytest.fixture
def crate_manifest() -> list[str | None]:
    """Collect the Cargo manifest a scenario writes."""
    return [None]


@given(
    "a crate whose child module assumes a condition",
    target_fixture="crate_source",
)
def crate_with_an_assumption(repo_root: Path) -> str:
    """Write a crate root that declares one child module assuming a condition."""
    _write(
        repo_root,
        {
            "src/lib.rs": "mod child;",
            "src/child.rs": "fn f() {\n    kani::assume(true);\n}",
        },
    )
    return "src/lib.rs"


@given(
    "a crate whose child module assumes nothing",
    target_fixture="crate_source",
)
def crate_without_assumptions(repo_root: Path) -> str:
    """Write a crate root whose child module contains no trusted item."""
    _write(
        repo_root,
        {
            "src/lib.rs": "mod child;",
            "src/child.rs": "fn f() {}",
        },
    )
    return "src/lib.rs"


@given(
    "a crate declaring a module that does not exist",
    target_fixture="crate_source",
)
def crate_with_a_missing_module(repo_root: Path) -> str:
    """Write a crate root whose declaration names no file."""
    _write(repo_root, {"src/lib.rs": "mod absent;"})
    return "src/lib.rs"


@given(
    "a crate whose single module asks both provers for trust",
    target_fixture="crate_source",
)
def crate_asking_both_provers(repo_root: Path) -> str:
    """Write a crate root importing both provers and using each one's surface."""
    _write(
        repo_root,
        {
            "src/lib.rs": "\n".join(
                (
                    "use vstd::prelude::*;",
                    "fn a() { admit(); }",
                    "fn b() { kani::assume(true); }",
                ),
            ),
        },
    )
    return "src/lib.rs"


@given(parsers.parse('the prover "{prover}" is selected'))
def prover_selected(selected_provers: list[str], prover: str) -> None:
    """Record one ``--prover`` selection."""
    selected_provers.append(prover)


@given(
    "a proc-macro crate whose literal holds a Kani call",
    target_fixture="crate_source",
)
def proc_macro_crate(repo_root: Path) -> str:
    """Write a crate the manifest declares as a proc-macro crate."""
    _write(
        repo_root,
        {
            "Cargo.toml": '[package]\nname = "macros"\nversion = "0.1.0"\n\n'
            "[lib]\nproc-macro = true\n",
            "src/lib.rs": 'fn f() { let _ = "kani::assume(x);"; }',
        },
    )
    return "src/lib.rs"


@given(
    "a source file outside the repository root",
    target_fixture="crate_source",
)
def source_outside_the_root(repo_root: Path) -> str:
    """Write a source beside the root, which the audit may not name."""
    outside = repo_root.parent / "outside.rs"
    outside.write_text("fn f() {}", encoding="utf-8")
    return str(outside)


def _write(root: Path, files: dict[str, str]) -> None:
    """Write each source beneath ``root``, creating its parent directory."""
    for relative, text in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def run_audit(
    repo_root: Path,
    source: str,
    *,
    selected_provers: list[str],
    output_json: bool,
) -> CliRun:
    """Run `audit trust` over one entry point."""
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "rust_prover_tools.cli",
            "audit",
            "trust",
            source,
            "--repo-root",
            str(repo_root),
            *(flag for prover in selected_provers for flag in ("--prover", prover)),
            *(["--format", "json"] if output_json else []),
        ],
        check=False,
        capture_output=True,
        text=True,
        env=sanitized_subprocess_env(),
    )
    return CliRun(completed.stdout, completed.stderr, completed.returncode)


@when("the user audits the crate", target_fixture="cli_run")
def user_audits_the_crate(
    repo_root: Path,
    crate_source: str,
    selected_provers: list[str],
) -> CliRun:
    """Audit the crate as text."""
    return run_audit(
        repo_root,
        crate_source,
        selected_provers=selected_provers,
        output_json=False,
    )


@when("the user audits the crate as JSON", target_fixture="cli_run")
def user_audits_the_crate_as_json(
    repo_root: Path,
    crate_source: str,
    selected_provers: list[str],
) -> CliRun:
    """Audit the crate as a JSON object."""
    return run_audit(
        repo_root,
        crate_source,
        selected_provers=selected_provers,
        output_json=True,
    )


@then("the command succeeds")
def command_succeeds(cli_run: CliRun) -> None:
    """Assert that the audit completed."""
    assert cli_run.returncode == 0, cli_run


@then("the command fails")
def command_fails(cli_run: CliRun) -> None:
    """Assert that the audit could not complete."""
    assert cli_run.returncode == 1, cli_run


@then(parsers.parse('stdout names the trusted item "{location}"'))
def stdout_names_the_item(cli_run: CliRun, location: str) -> None:
    """Assert the report carries the item at the location it was found."""
    assert f"{location}\n" in cli_run.stdout, cli_run.stdout


@then(parsers.parse('stdout reports "{line}"'))
def stdout_reports(cli_run: CliRun, line: str) -> None:
    """Assert the report contains one exact line."""
    assert f"{line}\n" in cli_run.stdout, cli_run.stdout


@then(
    parsers.parse(
        'stdout reports the JSON item "{path}" at line {line:d} column {column:d}',
    ),
)
def stdout_reports_json_item(
    cli_run: CliRun,
    path: str,
    line: int,
    column: int,
) -> None:
    """Assert the JSON payload carries the item and its location."""
    payload = json.loads(cli_run.stdout)
    assert payload["items"] == [
        {"item": "assume", "path": path, "line": line, "column": column},
    ], payload


@then(parsers.parse('stderr names the module "{name}"'))
def stderr_names_the_module(cli_run: CliRun, name: str) -> None:
    """Assert the diagnostic names the declaration that resolved to no file."""
    assert f"declares module '{name}'" in cli_run.stderr, cli_run.stderr


@then("stderr reports the file as outside the repository root")
def stderr_reports_outside_root(cli_run: CliRun) -> None:
    """Assert the diagnostic refuses a path the report could not name."""
    assert "is outside the repository root" in cli_run.stderr, cli_run.stderr


@then(parsers.parse('stdout reports the selected provers "{provers}"'))
def stdout_reports_selected_provers(cli_run: CliRun, provers: str) -> None:
    """Assert the report names the vocabularies it applied."""
    assert f"provers: {provers}\n" in cli_run.stdout, cli_run.stdout


@then(parsers.parse('stdout does not report the trusted word "{word}"'))
def stdout_omits_the_word(cli_run: CliRun, word: str) -> None:
    """Assert a word outside the selected vocabulary was not reported."""
    assert f": {word}\n" not in cli_run.stdout, cli_run.stdout
