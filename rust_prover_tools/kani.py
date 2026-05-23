"""Kani verifier workflow orchestration.

This module installs the pinned Kani verifier and checks that an installed
Kani command matches the repository pin. `install_kani` reads or accepts a
semantic version, runs the Cargo installation sequence, optionally runs
`cargo kani setup`, and optionally verifies the command. `check_kani_version`
runs the configured Kani command with `--version`, parses the first semantic
version, and compares it with the pin.

Example
-------
Create `KaniPaths(repo_root=Path.cwd())`, then pass `KaniInstallOptions` to
`install_kani` or `KaniCheckOptions` to `check_kani_version`. Both functions
return user-facing status text and raise `ProverToolError` or
`CommandFailedError` for expected failures.
"""

from __future__ import annotations

import dataclasses as dc
from pathlib import Path

from rust_prover_tools.commands import CommandSpec, run_checked, split_command
from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.versions import (
    parse_tool_version,
    read_version_pin,
    validate_semver,
)

KANI_VERSION_PATH = Path("tools/kani/VERSION")


@dc.dataclass(frozen=True, slots=True)
class KaniPaths:
    """Repository-relative Kani path configuration.

    Attributes
    ----------
    repo_root : Path
        Repository root used to resolve default Kani paths.
    version_file : Path | None
        Optional version file override. Defaults to `None`.

    Methods
    -------
    resolved_version_file
        Return `version_file` or `repo_root / KANI_VERSION_PATH`.
    """

    repo_root: Path
    version_file: Path | None = None

    def resolved_version_file(self) -> Path:
        """Return the configured or default version file.

        Returns
        -------
        Path
            Configured `version_file`, or `repo_root / KANI_VERSION_PATH` when
            no override is set.
        """
        if self.version_file is None:
            return self.repo_root / KANI_VERSION_PATH
        if self.version_file.is_absolute():
            return self.version_file
        return self.repo_root / self.version_file


@dc.dataclass(frozen=True, slots=True)
class KaniInstallOptions:
    """Options for installing Kani.

    Attributes
    ----------
    paths : KaniPaths
        Kani path configuration.
    version : str | None
        Optional semantic version override. Defaults to `None`.
    should_setup : bool
        Whether to run `cargo kani setup`. Defaults to `True`.
    should_verify : bool
        Whether to run `cargo kani --version`. Defaults to `True`.
    """

    paths: KaniPaths
    version: str | None = None
    should_setup: bool = True
    should_verify: bool = True


@dc.dataclass(frozen=True, slots=True)
class KaniCheckOptions:
    """Options for checking an installed Kani version.

    Attributes
    ----------
    paths : KaniPaths
        Kani path configuration.
    expected_version : str | None
        Optional semantic version override. Defaults to `None`.
    kani_command : str
        Command used to query Kani. Defaults to `cargo kani`.
    """

    paths: KaniPaths
    expected_version: str | None = None
    kani_command: str = "cargo kani"


def resolve_kani_version(paths: KaniPaths, override: str | None) -> str:
    """Resolve a Kani version from an override or version pin file.

    Parameters
    ----------
    paths : KaniPaths
        Path configuration used when no override is provided.
    override : str | None
        Optional version override.

    Returns
    -------
    str
        Validated semantic version string.

    Raises
    ------
    ProverToolError
        Raised when the override or version pin is invalid.
    """
    if override is not None:
        return validate_semver(override.strip())
    return read_version_pin(paths.resolved_version_file())


def install_kani(options: KaniInstallOptions) -> tuple[str, ...]:
    """Install the pinned Kani verifier and return user-facing output lines.

    Parameters
    ----------
    options : KaniInstallOptions
        Installation options including paths, version override, setup flag, and
        verification flag.

    Returns
    -------
    tuple[str, ...]
        User-facing status messages for completed steps.

    Raises
    ------
    ProverToolError
        Raised when version resolution fails.
    CommandFailedError
        Raised when a Cargo command exits non-zero.
    """
    version_file = options.paths.resolved_version_file()
    version = resolve_kani_version(options.paths, options.version)
    source = f"from {version_file}" if options.version is None else "(override)"
    lines = [f"Installing kani-verifier {version} {source}."]
    run_checked(
        CommandSpec((
            "cargo",
            "install",
            "--locked",
            "kani-verifier",
            "--version",
            version,
        )),
    )

    if options.should_setup:
        lines.append(f"Running cargo kani setup for kani-verifier {version}.")
        run_checked(CommandSpec(("cargo", "kani", "setup")))

    if options.should_verify:
        lines.append("Verifying cargo kani is callable.")
        result = run_checked(CommandSpec(("cargo", "kani", "--version")))
        verifier_output = result.stdout.strip()
        if verifier_output:
            lines.append(verifier_output)

    return tuple(lines)


def check_kani_version(options: KaniCheckOptions) -> str:
    """Check that the installed Kani command matches the expected version.

    Parameters
    ----------
    options : KaniCheckOptions
        Version check options including paths, expected version override, and
        command string.

    Returns
    -------
    str
        Success message in the form `Kani {actual} matches {version_file}.`.

    Raises
    ------
    ProverToolError
        Raised when the parsed version does not match the expected version.
    CommandFailedError
        Raised when the configured Kani command exits non-zero.
    """
    version_file = options.paths.resolved_version_file()
    expected = resolve_kani_version(options.paths, options.expected_version)
    source = (
        f"from {version_file}" if options.expected_version is None else "(override)"
    )
    command = split_command(options.kani_command)
    result = run_checked(CommandSpec((*command, "--version")))
    actual = parse_tool_version(result.stdout + result.stderr, tool_name="Kani")
    if actual != expected:
        msg = f"expected Kani {expected} {source}, found {actual}"
        raise ProverToolError(msg)
    return f"Kani {actual} matches {source}."
