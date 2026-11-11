"""Behavioural tests for the isolated Kani snapshot commands."""

from __future__ import annotations

import dataclasses as dc
import json
import os
import subprocess
import sys
import typing as typ
from pathlib import Path

from pytest_bdd import given, parsers, scenarios, then, when

from rust_prover_tools.kani_snapshot.pins import pin_to_toml
from tests.kani_snapshot_bundles import build_local_snapshot

if typ.TYPE_CHECKING:
    from rust_prover_tools.kani_snapshot.models import SnapshotManifest

scenarios("../features/kani_snapshot.feature")


@dc.dataclass(frozen=True, slots=True)
class SnapshotCase:
    """Paths for one reviewed snapshot and its private cache."""

    root: Path
    pin: Path
    assets: Path
    cache: Path
    shim: Path


@dc.dataclass(frozen=True, slots=True)
class CliRun:
    """Captured command-line run."""

    stdout: str
    stderr: str
    returncode: int


def _attestation_stub(manifest: SnapshotManifest) -> str:
    """Return a `gh` stand-in reporting one attestation for the manifest run."""
    invocation = (
        f"https://github.com/{manifest.repository}/actions/runs/"
        f"{manifest.workflow.run_id}/attempts/{manifest.workflow.run_attempt}"
    )
    record = json.dumps([
        {
            "verificationResult": {
                "statement": {
                    "predicate": {
                        "runDetails": {"metadata": {"invocationId": invocation}}
                    }
                }
            }
        }
    ])
    return f"#!/bin/sh\nprintf '%s\\n' '{record}'\n"


def _write_gh_stub(directory: Path, manifest: SnapshotManifest) -> Path:
    """Write a `gh` stand-in that reports the manifest's verified attestation."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "gh"
    path.write_text(_attestation_stub(manifest), encoding="utf-8")
    path.chmod(0o755)
    return path


@given(
    "a repository with a reviewed Kani snapshot pin",
    target_fixture="snapshot_case",
)
def reviewed_snapshot_pin(tmp_path: Path) -> SnapshotCase:
    """Write a reviewed pin and the complete local release assets."""
    snapshot = build_local_snapshot(tmp_path / "release")
    pin = tmp_path / "tools" / "kani" / "snapshot.toml"
    pin.parent.mkdir(parents=True)
    pin.write_bytes(pin_to_toml(snapshot.pin))
    return SnapshotCase(
        root=tmp_path,
        pin=pin,
        assets=snapshot.assets_dir,
        cache=tmp_path / "cache",
        shim=_write_gh_stub(tmp_path / "shim", snapshot.manifest).parent,
    )


def run_cli(case: SnapshotCase, *argv: str) -> CliRun:
    """Run the public CLI with the attestation stub first on `PATH`.

    Compiler admission routes are removed from the child. The snapshot probes
    the installed compiler through the caller's configured wrapper, so leaving
    a host build supervisor in place would queue every fixture probe behind
    unrelated compiler work and turn a behavioural test into a load test.
    """
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("INPUT_", "CARGO_BUILD_"))
        and key not in {"RUSTC_WRAPPER", "RUSTC_WORKSPACE_WRAPPER", "RUSTDOC"}
    }
    environment["PATH"] = os.pathsep.join((str(case.shim), environment.get("PATH", "")))
    completed = subprocess.run(
        [sys.executable, "-m", "rust_prover_tools.cli", *argv],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    return CliRun(
        stdout=completed.stdout,
        stderr=completed.stderr,
        returncode=completed.returncode,
    )


def install_command(case: SnapshotCase) -> CliRun:
    """Install the reviewed snapshot from its local asset directory."""
    return run_cli(
        case,
        "kani",
        "snapshot",
        "install",
        "--pin",
        str(case.pin),
        "--repo-root",
        str(case.root),
        "--cache-dir",
        str(case.cache),
        "--asset-dir",
        str(case.assets),
    )


def installed_generation(stdout: str) -> Path:
    """Return the generation directory named by an install summary."""
    for line in stdout.splitlines():
        if " installed at " in line:
            return Path(line.rsplit(" installed at ", 1)[1])
    msg = f"install output did not name a generation: {stdout!r}"
    raise AssertionError(msg)


@given("the snapshot is installed", target_fixture="previous_generation")
def snapshot_is_installed(snapshot_case: SnapshotCase) -> Path:
    """Install the snapshot before the scenario's action."""
    completed = install_command(snapshot_case)
    assert completed.returncode == 0, (
        f"fixture install failed: {completed.returncode}; "
        f"stdout: {completed.stdout}; stderr: {completed.stderr}"
    )
    return installed_generation(completed.stdout)


@given("the active generation receipt is corrupt")
def active_generation_receipt_is_corrupt(previous_generation: Path) -> None:
    """Make the active generation unreusable so install stages again."""
    (previous_generation / "receipt.json").write_text("{}", encoding="utf-8")


@given("the local snapshot assets change after installation")
def local_assets_change(snapshot_case: SnapshotCase) -> None:
    """Replace one published payload with bytes of the wrong size."""
    (snapshot_case.assets / "backend.tar.gz").write_bytes(b"tampered")


@when("the user installs the snapshot", target_fixture="cli_run")
def user_installs_snapshot(snapshot_case: SnapshotCase) -> CliRun:
    """Install the reviewed snapshot through the public CLI."""
    return install_command(snapshot_case)


@when("the user checks the snapshot", target_fixture="cli_run")
def user_checks_snapshot(snapshot_case: SnapshotCase) -> CliRun:
    """Check the active generation through the public CLI."""
    return run_cli(
        snapshot_case,
        "kani",
        "snapshot",
        "check",
        "--pin",
        str(snapshot_case.pin),
        "--repo-root",
        str(snapshot_case.root),
        "--cache-dir",
        str(snapshot_case.cache),
    )


def run_launcher(snapshot_case: SnapshotCase, *args: str) -> CliRun:
    """Run the pinned launcher with arguments forwarded after `--`."""
    return run_cli(
        snapshot_case,
        "kani",
        "run",
        "--snapshot-pin",
        str(snapshot_case.pin),
        "--repo-root",
        str(snapshot_case.root),
        "--cache-dir",
        str(snapshot_case.cache),
        "--",
        *args,
    )


@when(
    parsers.parse('the user runs the snapshot launcher with "{flag}" "{value}"'),
    target_fixture="cli_run",
)
def user_runs_snapshot_launcher(
    snapshot_case: SnapshotCase,
    flag: str,
    value: str,
) -> CliRun:
    """Forward a flag and its value to the installed launcher."""
    return run_launcher(snapshot_case, flag, value)


@when(
    "the user runs the upstream setup command through the snapshot",
    target_fixture="cli_run",
)
def user_runs_upstream_setup(snapshot_case: SnapshotCase) -> CliRun:
    """Ask the snapshot CLI to run upstream setup."""
    return run_launcher(snapshot_case, "setup")


@when(
    "the user selects the Lean backend through the snapshot", target_fixture="cli_run"
)
def user_selects_lean_backend(snapshot_case: SnapshotCase) -> CliRun:
    """Ask the snapshot CLI for the Lean backend."""
    return run_launcher(snapshot_case, "-Zlean")


@then("the command succeeds")
def command_succeeds(cli_run: CliRun) -> None:
    """Assert that the CLI succeeded."""
    assert cli_run.returncode == 0, (
        f"expected zero returncode, got {cli_run.returncode}; "
        f"stdout: {cli_run.stdout}; stderr: {cli_run.stderr}"
    )


@then("the command fails")
def command_fails(cli_run: CliRun) -> None:
    """Assert that the CLI failed."""
    assert cli_run.returncode != 0, (
        f"expected non-zero returncode; stdout: {cli_run.stdout}; "
        f"stderr: {cli_run.stderr}"
    )


@then("stdout reports the installed snapshot")
def stdout_reports_installed_snapshot(cli_run: CliRun) -> None:
    """Assert the install summary names the activated generation."""
    assert " installed at " in cli_run.stdout, (
        f"stdout did not report an installation: {cli_run.stdout!r}"
    )


@then("stdout reports the checked snapshot")
def stdout_reports_checked_snapshot(cli_run: CliRun) -> None:
    """Assert the check summary names the validated generation."""
    assert " checked at " in cli_run.stdout, (
        f"stdout did not report a check: {cli_run.stdout!r}"
    )


@then("the snapshot launcher exit status is 7")
def launcher_exit_status_is_seven(cli_run: CliRun) -> None:
    """Assert the launcher's own exit status survives the CLI."""
    assert cli_run.returncode == 7, (
        f"expected launcher exit status 7, got {cli_run.returncode}; "
        f"stdout: {cli_run.stdout}; stderr: {cli_run.stderr}"
    )


@then(parsers.parse('stdout contains the forwarded argument "{value}"'))
def stdout_contains_forwarded_argument(cli_run: CliRun, value: str) -> None:
    """Assert the launcher received the argument unchanged."""
    assert value in cli_run.stdout.splitlines(), (
        f"stdout did not forward {value!r}: {cli_run.stdout!r}"
    )


@then("stderr reports the changed snapshot asset")
def stderr_reports_changed_asset(cli_run: CliRun) -> None:
    """Assert the rejected payload is reported before setup runs."""
    assert "wrong size" in cli_run.stderr, (
        f"stderr did not report the changed asset: {cli_run.stderr!r}"
    )


@then("the previous generation directory is preserved")
def previous_generation_is_preserved(previous_generation: Path) -> None:
    """Assert a failed attempt never removes the previous generation."""
    assert previous_generation.is_dir(), "the previous generation was removed"
    assert (previous_generation / "receipt.json").is_file(), "the receipt was removed"


@then("stderr refuses the setup command")
def stderr_refuses_setup(cli_run: CliRun) -> None:
    """Assert upstream setup was refused."""
    assert "setup command" in cli_run.stderr, (
        f"stderr did not refuse setup: {cli_run.stderr!r}"
    )


@then("stderr refuses the Lean backend")
def stderr_refuses_lean(cli_run: CliRun) -> None:
    """Assert the Lean backend was refused."""
    assert "Lean backend" in cli_run.stderr, (
        f"stderr did not refuse Lean: {cli_run.stderr!r}"
    )
