"""Regression contracts for reviewed Kani runtime boundaries."""

from __future__ import annotations

import logging
import os
import typing as typ

import pytest

from rust_prover_tools import kani_release
from rust_prover_tools.commands import CommandResult
from rust_prover_tools.errors import CommandFailedError, ProverToolError
from rust_prover_tools.kani_release import KaniExecOptions, prepare_kani_command
from rust_prover_tools.unittests.test_kani_release import (
    installed_release as installed_release,
)
from rust_prover_tools.unittests.test_kani_release import (
    owned_config_discovery as owned_config_discovery,
)

if typ.TYPE_CHECKING:
    from pathlib import Path

    from rust_prover_tools.commands import CommandSpec


@pytest.mark.parametrize("variable", ["PATH", "PYTHONPATH"])
@pytest.mark.parametrize("inherited", [None, "", "/kept:first::last:"])
def test_search_paths_do_not_introduce_empty_components(
    installed_release: Path, tmp_path: Path, variable: str, inherited: str | None
) -> None:
    """Only existing nonempty inherited search strings are appended verbatim."""
    environment = {"CARGO_HOME": str(tmp_path / "cargo-home")}
    if inherited is not None:
        environment[variable] = inherited
    spec = prepare_kani_command(
        KaniExecOptions(installed_release, tmp_path, ("list",)), environment
    )
    assert spec.env is not None
    prefixes = (
        ("toolchain/bin", "bin", "pyroot/bin") if variable == "PATH" else ("pyroot",)
    )
    expected = tuple(str(installed_release / value) for value in prefixes)
    if inherited:
        expected = (*expected, inherited)
    assert spec.env[variable] == os.pathsep.join(expected)


@pytest.mark.parametrize("machine", ["aarch64", "i686"])
def test_unsupported_linux_architecture_stops_before_any_child(
    installed_release: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    machine: str,
) -> None:
    """An incompatible Linux architecture fails at validation, not exec format."""
    monkeypatch.setattr(kani_release.platform, "machine", lambda: machine)

    def unexpected(_spec: CommandSpec) -> CommandResult:
        pytest.fail("unsupported architecture reached a child command")

    monkeypatch.setattr(kani_release, "run_command", unexpected)
    with pytest.raises(ProverToolError, match="x86_64"):
        kani_release.exec_kani(
            KaniExecOptions(installed_release, tmp_path, ("list",)),
            {"CARGO_HOME": str(tmp_path / "cargo-home")},
        )


@pytest.mark.parametrize("version", ["0.67.0-rc1", "0.67.0+meta", "0.67.0suffix"])
def test_suffixed_verifier_identity_never_reaches_proof(
    installed_release: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    version: str,
) -> None:
    """Matching base digits cannot qualify a prerelease or another suffix."""
    seen: list[CommandSpec] = []

    def capture(spec: CommandSpec) -> CommandResult:
        seen.append(spec)
        assert spec.argv[-1] == "--version", "proof must not start"
        identity = (
            f"kani {version}"
            if spec.argv[0].endswith("/kani-driver")
            else "rustc 1.93.0-nightly (53732d5e0 2025-11-20)"
        )
        return CommandResult(identity, "", 0)

    monkeypatch.setattr(kani_release, "run_command", capture)
    with pytest.raises(ProverToolError, match="verifier identity"):
        kani_release.exec_kani(
            KaniExecOptions(installed_release, tmp_path, ("list",)),
            {"CARGO_HOME": str(tmp_path / "cargo-home")},
        )
    assert len(seen) == 3


@pytest.mark.parametrize("kind", ["missing", "file"])
def test_invalid_proof_directory_stops_before_any_child(
    installed_release: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    """A missing cwd or regular file produces the typed validation failure."""
    cwd = tmp_path / kind
    if kind == "file":
        cwd.touch()

    def unexpected(_spec: CommandSpec) -> CommandResult:
        pytest.fail("invalid working directory reached a child command")

    monkeypatch.setattr(kani_release, "run_command", unexpected)
    with pytest.raises(ProverToolError, match="working directory"):
        kani_release.exec_kani(
            KaniExecOptions(installed_release, cwd, ("list",)),
            {"CARGO_HOME": str(tmp_path / "cargo-home")},
        )


def test_setup_harness_filter_is_forwarded_literally(
    installed_release: Path, tmp_path: Path
) -> None:
    """The Kani0.67 string-valued harness option may name a harness setup."""
    spec = prepare_kani_command(
        KaniExecOptions(installed_release, tmp_path, ("--harness", "setup")),
        {"CARGO_HOME": str(tmp_path / "cargo-home")},
    )
    assert spec.argv[-2:] == ("--harness", "setup")


@pytest.mark.parametrize("executable", ["cargo", "rustc"])
def test_toolchain_executable_cannot_escape_selected_toolchain(
    installed_release: Path, tmp_path: Path, executable: str
) -> None:
    """A file symlink cannot select another Cargo or compiler outside the toolchain."""
    path = installed_release / "toolchain/bin" / executable
    other = tmp_path / executable
    path.rename(other)
    path.symlink_to(other)
    with pytest.raises(ProverToolError, match="selected toolchain"):
        prepare_kani_command(
            KaniExecOptions(installed_release, tmp_path, ("list",)),
            {"CARGO_HOME": str(tmp_path / "cargo-home")},
        )


def test_release_toolchain_directory_may_link_to_shared_rustup(
    installed_release: Path, tmp_path: Path
) -> None:
    """Kani setup's standard shared-toolchain directory symlink remains valid."""
    toolchain = installed_release / "toolchain"
    shared = tmp_path / "rustup-toolchain"
    toolchain.rename(shared)
    toolchain.symlink_to(shared, target_is_directory=True)
    spec = prepare_kani_command(
        KaniExecOptions(installed_release, tmp_path, ("list",)),
        {"CARGO_HOME": str(tmp_path / "cargo-home")},
    )
    assert spec.argv[0] == str(installed_release / "bin/kani-driver")


@pytest.mark.parametrize(
    ("status", "category"), [(0, "success"), (23, "nonzero"), (-15, "signal")]
)
def test_execution_logs_bounded_stage_fields_without_payloads(
    installed_release: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    status: int,
    category: str,
) -> None:
    """An application logging sink observes stages without private proof content."""
    caplog.set_level(logging.INFO, logger="rust_prover_tools.kani_release")

    def capture(spec: CommandSpec) -> CommandResult:
        if spec.argv[-1] != "--version":
            return CommandResult("private-proof-output", "private-proof-error", status)
        identity = (
            "cargo-kani 0.67.0"
            if spec.argv[0].endswith("/kani-driver")
            else "rustc 1.93.0-nightly (53732d5e0 2025-11-20)"
        )
        return CommandResult(identity, "", 0)

    monkeypatch.setattr(kani_release, "run_command", capture)
    assert (
        kani_release.exec_kani(
            KaniExecOptions(installed_release, tmp_path, ("private-harness",)),
            {
                "CARGO_HOME": str(tmp_path / "cargo-home"),
                "PRIVATE_TOKEN": "secret-value",
            },
        )
        == status
    )
    records = [r for r in caplog.records if r.name == "rust_prover_tools.kani_release"]
    assert [r.__dict__["stage"] for r in records] == [
        "validation",
        "rustc_identity",
        "compiler_identity",
        "verifier_identity",
        "proof",
    ]
    assert [r.__dict__["status_class"] for r in records] == ["success"] * 4 + [category]
    assert records[-1].__dict__["error_category"] == (
        "none" if status == 0 else "command"
    )
    for record in records:
        assert record.__dict__["release"] == "0.67.0"
        assert record.__dict__["duration_seconds"] >= 0
        assert record.getMessage() == "Kani execution stage completed"
        assert record.exc_info is None
        assert record.args == ()
    for sensitive in (
        str(installed_release),
        "private-harness",
        "secret-value",
        "private-proof",
    ):
        assert sensitive not in caplog.text


def test_validation_failure_logs_one_sanitized_stage(
    installed_release: Path,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Validation failure is recorded once without logging the typed message."""
    caplog.set_level(logging.INFO, logger="rust_prover_tools.kani_release")
    with pytest.raises(ProverToolError, match="working directory"):
        kani_release.exec_kani(
            KaniExecOptions(installed_release, tmp_path / "private-missing", ("list",)),
            {"CARGO_HOME": str(tmp_path / "cargo-home")},
        )
    records = [r for r in caplog.records if r.name == "rust_prover_tools.kani_release"]
    assert len(records) == 1
    assert records[0].__dict__["stage"] == "validation"
    assert records[0].__dict__["status_class"] == "failure"
    assert records[0].__dict__["error_category"] == "validation"
    assert "private-missing" not in caplog.text


def test_identity_signal_logs_one_failure_without_diagnostic_payload(
    installed_release: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A failed admitted query records its category without leaking stderr."""
    caplog.set_level(logging.INFO, logger="rust_prover_tools.kani_release")

    def capture(_spec: CommandSpec) -> CommandResult:
        return CommandResult("", "private-identity-error", -15)

    monkeypatch.setattr(kani_release, "run_command", capture)
    with pytest.raises(CommandFailedError) as failure:
        kani_release.exec_kani(
            KaniExecOptions(installed_release, tmp_path, ("list",)),
            {"CARGO_HOME": str(tmp_path / "cargo-home")},
        )
    assert failure.value.exit_code == -15
    records = [r for r in caplog.records if r.name == "rust_prover_tools.kani_release"]
    assert [r.__dict__["stage"] for r in records] == ["validation", "rustc_identity"]
    assert records[-1].__dict__["status_class"] == "signal"
    assert records[-1].__dict__["error_category"] == "command"
    assert "private-identity-error" not in caplog.text
