"""Tests for Kani workflow orchestration.

This file exercises Kani installation and version-check workflows through the
same Cuprum path as production code. The tests use the cmd-mox command-mocking
framework and temporary version pins to assert command order, optional flags,
override handling, and error propagation without running real Cargo commands.
"""

from __future__ import annotations

import pathlib
import typing as typ

import pytest

from rust_prover_tools.errors import CommandFailedError, ProverToolError
from rust_prover_tools.kani import (
    KaniCheckOptions,
    KaniInstallOptions,
    KaniPaths,
    check_kani_version,
    install_kani,
    resolve_kani_version,
)
from rust_prover_tools.versions import read_version_pin

if typ.TYPE_CHECKING:
    from pathlib import Path

    from cmd_mox import CmdMox, Invocation

pytest_plugins = ("cmd_mox.pytest_plugin",)


def test_install_kani_runs_install_setup_and_verify(
    tmp_path: Path,
    cmd_mox: CmdMox,
) -> None:
    """Kani installation preserves the shell script command sequence."""
    version_file = tmp_path / "tools" / "kani" / "VERSION"
    version_file.parent.mkdir(parents=True)
    version_file.write_text("0.61.0\n", encoding="utf-8")

    def cargo_handler(invocation: Invocation) -> tuple[str, str, int]:
        if invocation.args == ["kani", "--version"]:
            return ("kani 0.61.0\n", "", 0)
        return ("", "", 0)

    cmd_mox.stub("cargo").runs(cargo_handler)

    lines = install_kani(KaniInstallOptions(paths=KaniPaths(repo_root=tmp_path)))

    assert lines == (
        f"Installing kani-verifier 0.61.0 from {version_file}.",
        "Running cargo kani setup for kani-verifier 0.61.0.",
        "Verifying cargo kani is callable.",
        "kani 0.61.0",
    ), "Install should return status messages for all three steps"
    assert [call.args for call in cmd_mox.journal] == [
        ["install", "--locked", "kani-verifier", "--version", "0.61.0"],
        ["kani", "setup"],
        ["kani", "--version"],
    ], "Should execute cargo commands in correct sequence"


def test_check_kani_version_accepts_matching_override_command(
    tmp_path: Path,
    cmd_mox: CmdMox,
) -> None:
    """Kani version checks support quoted command overrides."""
    version_file = tmp_path / "VERSION"
    version_file.write_text("0.61.0\n", encoding="utf-8")
    cmd_mox.mock("cargo").with_args(
        "kani",
        "--format=json",
        "--version",
    ).returns(stdout="kani 0.61.0\n")

    message = check_kani_version(
        KaniCheckOptions(
            paths=KaniPaths(repo_root=tmp_path, version_file=version_file),
            kani_command='cargo kani "--format=json"',
        ),
    )

    assert message == f"Kani 0.61.0 matches from {version_file}.", (
        "Check should succeed when versions match"
    )


def test_check_kani_version_resolves_relative_version_file_from_repo_root(
    tmp_path: Path,
    cmd_mox: CmdMox,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Relative Kani version pins resolve against the configured repository."""
    repo_root = tmp_path / "repo-root"
    version_file = repo_root / "tools" / "kani" / "VERSION"
    version_file.parent.mkdir(parents=True)
    version_file.write_text("0.61.0\n", encoding="utf-8")
    unrelated_cwd = tmp_path / "unrelated"
    unrelated_cwd.mkdir()
    monkeypatch.chdir(unrelated_cwd)
    cmd_mox.mock("cargo").with_args("kani", "--version").returns(
        stdout="kani 0.61.0\n",
    )

    message = check_kani_version(
        KaniCheckOptions(
            paths=KaniPaths(
                repo_root=repo_root,
                version_file=pathlib.Path("tools/kani/VERSION"),
            ),
        ),
    )

    assert message == f"Kani 0.61.0 matches from {version_file}.", (
        "Relative version_file should resolve below repo_root, not cwd"
    )


def test_install_kani_can_skip_setup_and_verify(
    tmp_path: Path,
    cmd_mox: CmdMox,
) -> None:
    """Optional install flags omit setup and verification commands."""
    version_file = tmp_path / "tools" / "kani" / "VERSION"
    version_file.parent.mkdir(parents=True)
    version_file.write_text("0.61.0\n", encoding="utf-8")
    cmd_mox.mock("cargo").with_args(
        "install",
        "--locked",
        "kani-verifier",
        "--version",
        "0.61.0",
    ).returns()

    lines = install_kani(
        KaniInstallOptions(
            paths=KaniPaths(repo_root=tmp_path),
            should_setup=False,
            should_verify=False,
        ),
    )

    assert lines == (f"Installing kani-verifier 0.61.0 from {version_file}.",), (
        "Skipped optional flags should leave only the install status line"
    )
    assert [call.args for call in cmd_mox.journal] == [
        ["install", "--locked", "kani-verifier", "--version", "0.61.0"],
    ], "Setup and verification commands should be omitted"


def test_install_kani_reports_version_override_source(
    tmp_path: Path,
    cmd_mox: CmdMox,
) -> None:
    """Install status output says when the version came from an override."""
    cmd_mox.mock("cargo").with_args(
        "install",
        "--locked",
        "kani-verifier",
        "--version",
        "0.61.0",
    ).returns()

    lines = install_kani(
        KaniInstallOptions(
            paths=KaniPaths(repo_root=tmp_path),
            version="0.61.0",
            should_setup=False,
            should_verify=False,
        ),
    )

    assert lines == ("Installing kani-verifier 0.61.0 (override).",), (
        "Override installs should not claim the version came from a file"
    )


def test_check_kani_version_rejects_mismatched_version(
    tmp_path: Path,
    cmd_mox: CmdMox,
) -> None:
    """Version checks fail when the installed Kani version differs."""
    version_file = tmp_path / "tools" / "kani" / "VERSION"
    version_file.parent.mkdir(parents=True)
    version_file.write_text("0.61.0\n", encoding="utf-8")
    cmd_mox.mock("cargo").with_args("kani", "--version").returns(
        stdout="kani 0.60.0\n",
    )

    with pytest.raises(
        ProverToolError,
        match=f"expected Kani 0.61.0 from {version_file}, found 0.60.0",
    ):
        check_kani_version(KaniCheckOptions(paths=KaniPaths(repo_root=tmp_path)))


def test_check_kani_version_reports_expected_override_source(
    tmp_path: Path,
    cmd_mox: CmdMox,
) -> None:
    """Mismatch output says when the expected version came from an override."""
    cmd_mox.mock("cargo").with_args("kani", "--version").returns(
        stdout="kani 0.60.0\n",
    )

    with pytest.raises(
        ProverToolError,
        match=r"expected Kani 0.61.0 [(]override[)], found 0.60.0",
    ):
        check_kani_version(
            KaniCheckOptions(
                paths=KaniPaths(repo_root=tmp_path),
                expected_version="0.61.0",
            ),
        )


def test_resolve_kani_version_rejects_malformed_override(tmp_path: Path) -> None:
    """Malformed explicit version overrides fail before command execution."""
    with pytest.raises(
        ProverToolError,
        match=r"version pin '0[.]61' must use MAJOR[.]MINOR[.]PATCH format",
    ):
        resolve_kani_version(KaniPaths(repo_root=tmp_path), "0.61")


def test_install_kani_propagates_failed_cargo_install(
    tmp_path: Path,
    cmd_mox: CmdMox,
) -> None:
    """Cargo install failures propagate as command failures."""
    version_file = tmp_path / "tools" / "kani" / "VERSION"
    version_file.parent.mkdir(parents=True)
    version_file.write_text("0.61.0\n", encoding="utf-8")
    cmd_mox.mock("cargo").with_args(
        "install",
        "--locked",
        "kani-verifier",
        "--version",
        "0.61.0",
    ).returns(stderr="install failed\n", exit_code=2)

    with pytest.raises(CommandFailedError, match="command failed"):
        install_kani(KaniInstallOptions(paths=KaniPaths(repo_root=tmp_path)))


def test_check_kani_version_propagates_failed_version_command(
    tmp_path: Path,
    cmd_mox: CmdMox,
) -> None:
    """Kani command failures propagate as command failures."""
    version_file = tmp_path / "tools" / "kani" / "VERSION"
    version_file.parent.mkdir(parents=True)
    version_file.write_text("0.61.0\n", encoding="utf-8")
    cmd_mox.mock("cargo").with_args("kani", "--version").returns(
        stderr="version failed\n",
        exit_code=2,
    )

    with pytest.raises(CommandFailedError, match="command failed"):
        check_kani_version(KaniCheckOptions(paths=KaniPaths(repo_root=tmp_path)))


def test_read_version_pin_rejects_missing_version_file(tmp_path: Path) -> None:
    """Missing Kani version pins are user-facing errors."""
    missing_version = tmp_path / "tools" / "kani" / "VERSION"

    with pytest.raises(ProverToolError, match="does not exist"):
        read_version_pin(missing_version)


def test_check_kani_version_rejects_empty_version_file(tmp_path: Path) -> None:
    """Empty Kani version pins are rejected before command execution."""
    version_file = tmp_path / "tools" / "kani" / "VERSION"
    version_file.parent.mkdir(parents=True)
    version_file.write_text(" \n\t\n", encoding="utf-8")

    with pytest.raises(ProverToolError, match="is empty"):
        check_kani_version(KaniCheckOptions(paths=KaniPaths(repo_root=tmp_path)))
