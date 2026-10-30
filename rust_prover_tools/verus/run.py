"""Verus proof execution and toolchain helpers.

This module resolves a Verus binary, checks the Rust toolchain required by
`verus --version`, optionally installs that toolchain with `rustup`, and runs
the requested proof files one at a time. It is useful for CLI code and tests
that need the runner behaviour without reimplementing the shell script's
fallback rules.

Use `run_verus` for the complete proof workflow. Import `resolve_verus_bin`,
`ensure_verus_toolchain`, or `ensure_toolchain_installed` when a caller needs
to validate an individual step. External commands are executed through Cuprum,
which keeps production execution and cmd-mox-backed tests on the same path.

Example
-------
```python
from pathlib import Path

from rust_prover_tools.verus.models import VerusPaths, VerusRunOptions
from rust_prover_tools.verus.run import run_verus

run_verus(
    VerusRunOptions(
        paths=VerusPaths(repo_root=Path.cwd()),
        proof_files=(Path("verus/edge_harvest_proofs.rs"),),
    ),
)
```
"""

from __future__ import annotations

import shutil
from pathlib import Path

from rust_prover_tools.commands import CommandSpec, run_checked, run_command
from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.versions import parse_verus_toolchain
from rust_prover_tools.verus.batch import VerusRunSettings, verify_proof_files
from rust_prover_tools.verus.install import install_verus
from rust_prover_tools.verus.models import (
    EXAMPLE_VERUS_PROOF_PATH,
    VerusInstallOptions,
    VerusRunOptions,
    default_install_dir,
    is_executable_file,
    read_verus_version,
)


def resolve_verus_bin(candidate: str) -> Path | None:
    """Resolve a Verus binary from a file, directory, or command name.

    Parameters
    ----------
    candidate : str
        Binary override. It may be an executable file, a directory containing a
        supported Verus layout, or a command name on `PATH`.

    Returns
    -------
    Path | None
        Resolved executable path, or `None` when no executable is found.

    Notes
    -----
    Directory candidates are checked for `verus`, `verus/verus`, and
    `bin/verus` before falling back to `PATH` lookup with `shutil.which`.
    """
    if not candidate:
        return None
    candidate_path = Path(candidate)
    if candidate_path.is_dir():
        for child in ("verus", "verus/verus", "bin/verus"):
            resolved = candidate_path / child
            if is_executable_file(resolved):
                return resolved
        return None
    if is_executable_file(candidate_path):
        return candidate_path
    command_path = shutil.which(candidate)
    if command_path is not None:
        return Path(command_path)
    return None


def run_verus(options: VerusRunOptions) -> tuple[str, ...]:
    """Run Verus against each requested proof file.

    Parameters
    ----------
    options : VerusRunOptions
        Proof-run options including repository paths, proof files, optional
        Verus binary, toolchain behaviour, install fallback behaviour, and
        extra verifier arguments.

    Returns
    -------
    tuple[str, ...]
        Wrapper messages emitted by the Python runner, such as fallback
        warnings. The Verus verifier's own output is streamed by Cuprum when
        the proof command runs.

    Raises
    ------
    ProverToolError
        Raised when a proof file, the Verus binary, or the toolchain output is
        missing or invalid.
    VerusProofFailedError
        Raised when the verifier exits non-zero for any proof file; its exit
        code is preserved for the CLI and the message names the failing file
        together with the files proved before it.
    CommandFailedError
        Raised when an installer or `rustup` command exits non-zero.

    Notes
    -----
    Verus rejects more than one input filename per invocation, so a
    multi-file run is a sequence of verifier invocations in the order given.
    Every proof file is checked for existence before the first invocation
    starts, which keeps a mistyped path from being reported as a partial proof
    run. The loop stops at the first failing file so the prover's exit status
    survives.
    """
    proof_files = resolve_proof_files(options)
    resolved, lines = ensure_run_binary(options)
    toolchain = ensure_verus_toolchain(
        resolved,
        should_install=options.should_ensure_toolchain,
    )
    verify_proof_files(
        proof_files,
        VerusRunSettings(resolved, toolchain, options.extra_args),
    )
    return lines


def resolve_proof_files(options: VerusRunOptions) -> tuple[Path, ...]:
    """Resolve the proof files to verify.

    Parameters
    ----------
    options : VerusRunOptions
        Run options carrying the requested proof files and repository paths.

    Returns
    -------
    tuple[Path, ...]
        Proof files to verify, defaulting to `EXAMPLE_VERUS_PROOF_PATH` below
        the repository root when none were requested.

    Raises
    ------
    ProverToolError
        Raised when any proof file is missing. Every path is checked before
        verification starts, so a mistyped path cannot leave a partial run.
    """
    proof_files = options.proof_files or (
        options.paths.repo_root / EXAMPLE_VERUS_PROOF_PATH,
    )
    for proof_file in proof_files:
        if not proof_file.is_file():
            msg = f"Verus proof file not found: {proof_file}"
            raise ProverToolError(msg)
    return proof_files


def ensure_run_binary(options: VerusRunOptions) -> tuple[Path, tuple[str, ...]]:
    """Ensure a usable Verus binary exists for a proof run.

    Parameters
    ----------
    options : VerusRunOptions
        Run options carrying the binary override, install directory, and
        fallback behaviour.

    Returns
    -------
    tuple[Path, tuple[str, ...]]
        Resolved executable and any wrapper warning lines describing a
        fallback from an invalid explicit override.

    Raises
    ------
    ProverToolError
        Raised when no executable Verus binary can be resolved, even after the
        install fallback.

    Notes
    -----
    The name records the side effect: when the default binary is missing and
    `options.should_install_missing` is true, this function may download and
    install Verus before returning. Callers that need a read-only probe should
    use `resolve_verus_bin` instead.
    """
    version = read_verus_version(options.paths)
    default_bin = (
        (options.install_dir or default_install_dir(options.paths, version))
        / "verus"
        / "verus"
    )
    lines: tuple[str, ...] = ()
    resolved = resolve_verus_bin(options.verus_bin or str(default_bin))
    if resolved is None and options.verus_bin not in {None, str(default_bin)}:
        lines += fallback_warning(options.verus_bin or "", default_bin)
        resolved = resolve_default_verus(options, default_bin)
    elif resolved is None:
        resolved = resolve_default_verus(options, default_bin)

    if resolved is None or not is_executable_file(resolved):
        msg = f"Verus binary not found after install: {default_bin}"
        raise ProverToolError(msg)
    return resolved, lines


def fallback_warning(verus_bin: str, default_bin: Path) -> tuple[str, ...]:
    """Return warning lines for an invalid explicit Verus binary override.

    Parameters
    ----------
    verus_bin : str
        User-provided Verus binary path, directory, or command override.
    default_bin : Path
        Default Verus binary path used as the fallback target.

    Returns
    -------
    tuple[str, ...]
        Warning lines: first the invalid override reason, then the fallback
        notice.
    """
    candidate = Path(verus_bin)
    if candidate.is_dir():
        warning = (
            f"VERUS_BIN directory contains no recognised Verus binary: {verus_bin}"
        )
    else:
        warning = f"VERUS_BIN is not executable: {verus_bin}"
    return (warning, f"Falling back to {default_bin}")


def resolve_default_verus(options: VerusRunOptions, default_bin: Path) -> Path | None:
    """Resolve or install the default Verus binary.

    Parameters
    ----------
    options : VerusRunOptions
        Run options containing install fallback behaviour and paths.
    default_bin : Path
        Expected default Verus binary path.

    Returns
    -------
    Path | None
        Resolved or installed binary path, or `None` if it remains missing.

    Notes
    -----
    When `options.should_install_missing` is true this function may call
    `install_verus`, then re-check the binary with `resolve_verus_bin`.
    """
    found = resolve_verus_bin(str(default_bin))
    if found is not None:
        return found
    if options.should_install_missing:
        install_verus(
            VerusInstallOptions(
                paths=options.paths,
                target=options.target,
                install_dir=options.install_dir,
            ),
        )
    return resolve_verus_bin(str(default_bin))


def ensure_verus_toolchain(verus_bin: Path, *, should_install: bool) -> str:
    """Ensure the Rust toolchain required by Verus is available.

    Parameters
    ----------
    verus_bin : Path
        Path to the Verus binary whose `--version` output declares the
        toolchain.
    should_install : bool
        Whether to call `ensure_toolchain_installed` when the version probe
        fails after declaring a missing toolchain.

    Returns
    -------
    str
        Parsed Rust toolchain identifier returned by `parse_verus_toolchain`.

    Raises
    ------
    ProverToolError
        Raised when `verus --version` fails and `should_install` is false, or
        when it still fails after `ensure_toolchain_installed` reruns it.

    Notes
    -----
    The function always runs `verus_bin --version`; with `should_install` true
    it installs the parsed toolchain only when the initial version probe fails.
    """
    version_result = run_command(CommandSpec((str(verus_bin), "--version")))
    version_output = version_result.stdout + version_result.stderr
    toolchain = parse_verus_toolchain(version_output)
    if version_result.exit_code == 0:
        return toolchain
    if should_install:
        ensure_toolchain_installed(toolchain)
        rerun = run_command(CommandSpec((str(verus_bin), "--version")))
        if rerun.exit_code != 0:
            msg = (
                f"Failed to run {verus_bin} --version after installing toolchain.\n"
                f"{rerun.stdout}{rerun.stderr}"
            )
            raise ProverToolError(msg)
        return parse_verus_toolchain(rerun.stdout + rerun.stderr)
    raise ProverToolError(version_output)


def ensure_toolchain_installed(toolchain: str) -> None:
    """Install a Rust toolchain when `rustup` reports it missing.

    Parameters
    ----------
    toolchain : str
        Rust toolchain identifier to check with `rustup`.

    Raises
    ------
    ProverToolError
        Raised when `rustup` is not available on `PATH`.
    CommandFailedError
        Raised by `run_checked` when `rustup toolchain install` exits
        non-zero.
    """
    if shutil.which("rustup") is None:
        msg = f"rustup is required to install toolchain {toolchain}"
        raise ProverToolError(msg)
    check = run_command(
        CommandSpec(("rustup", "which", "--toolchain", toolchain, "rustc")),
    )
    if check.exit_code != 0:
        run_checked(CommandSpec(("rustup", "toolchain", "install", toolchain)))
