"""Command-line interface for Rust prover tooling.

This module exposes the `prover-tools` console script. Commands are grouped by
tool noun: `kani` handles Kani installation and version checks, while `verus`
handles Verus installation and proof execution.

Examples
--------
Run the Kani version check:

```bash
prover-tools kani check-version
```

Run Verus against a proof file:

```bash
prover-tools verus run --proof-file verus/edge_harvest_proofs.rs
```

Configuration can come from flags, `INPUT_` environment variables, or the
legacy `KANI` and `VERUS_*` variables preserved from the shell scripts.
"""

from __future__ import annotations

import sys
import typing as typ
from pathlib import Path

import cyclopts
from cyclopts import App, Parameter

from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani import (
    KaniCheckOptions,
    KaniInstallOptions,
    KaniPaths,
    check_kani_version,
    install_kani,
)
from rust_prover_tools.verus import (
    DEFAULT_TARGET,
    VERUS_BASE_URL,
    VerusInstallOptions,
    VerusPaths,
    VerusProofFailedError,
    VerusRunOptions,
    install_verus,
    run_verus,
)

app: cyclopts.App = App(
    "prover-tools",
    config=cyclopts.config.Env("INPUT_", command=False),
    help_format="plaintext",
    result_action="return_value",
)
kani_app: cyclopts.App = App(
    "kani",
    help="Manage Kani verifier installation and version checking",
)
verus_app: cyclopts.App = App(
    "verus",
    help="Manage Verus verifier installation and proof execution",
)


def _repo_root(path: Path | None) -> Path:
    """Return the requested repository root or the current working directory."""
    return (path or Path.cwd()).resolve()


def _print_lines(lines: tuple[str, ...], *, stream: typ.TextIO = sys.stdout) -> None:
    """Print lines to a text stream."""
    for line in lines:
        print(line, file=stream)


def _error_exit(exc: ProverToolError) -> int:
    """Print a user-facing error and return the appropriate exit status."""
    print(exc, file=sys.stderr)
    match exc:
        case VerusProofFailedError(exit_code=code):
            return code
        case _:
            return 1


@kani_app.command
# CLI parameter binding needs one explicit parameter per flag/env-var mapping.
# pylint: disable-next=too-many-arguments
def install(
    *,
    repo_root: typ.Annotated[Path | None, Parameter(env_var="INPUT_REPO_ROOT")] = None,
    version_file: typ.Annotated[
        Path | None,
        Parameter(env_var=("INPUT_VERSION_FILE", "INPUT_KANI_VERSION_FILE")),
    ] = None,
    version: typ.Annotated[str | None, Parameter(env_var="INPUT_VERSION")] = None,
    setup: typ.Annotated[bool, Parameter(negative="--no-setup")] = True,
    verify: typ.Annotated[bool, Parameter(negative="--no-verify")] = True,
) -> int:
    """Install the pinned Kani verifier.

    Parameters
    ----------
    repo_root : Path | None
        Repository root used to resolve default tool pin paths.
    version_file : Path | None
        Optional Kani version pin override.
    version : str | None
        Optional semantic version override.
    setup : bool
        Whether to run `cargo kani setup` after installation.
    verify : bool
        Whether to run `cargo kani --version` after installation.

    Returns
    -------
    int
        Exit code, where `0` indicates success.

    Raises
    ------
    ProverToolError
        Handled internally and translated to a non-zero exit code.
    """
    try:
        lines = install_kani(
            KaniInstallOptions(
                paths=KaniPaths(
                    repo_root=_repo_root(repo_root), version_file=version_file
                ),
                version=version,
                should_setup=setup,
                should_verify=verify,
            ),
        )
    except ProverToolError as exc:
        return _error_exit(exc)
    _print_lines(lines)
    return 0


@kani_app.command
def check_version(
    *,
    repo_root: typ.Annotated[Path | None, Parameter(env_var="INPUT_REPO_ROOT")] = None,
    version_file: typ.Annotated[
        Path | None,
        Parameter(env_var=("INPUT_VERSION_FILE", "INPUT_KANI_VERSION_FILE")),
    ] = None,
    expected_version: typ.Annotated[
        str | None,
        Parameter(env_var="INPUT_EXPECTED_VERSION"),
    ] = None,
    kani_command: typ.Annotated[
        str,
        Parameter(env_var=("KANI", "INPUT_KANI_COMMAND")),
    ] = "cargo kani",
) -> int:
    """Check that the installed Kani version matches the pin.

    Parameters
    ----------
    repo_root : Path | None
        Repository root used to resolve default tool pin paths.
    version_file : Path | None
        Optional Kani version pin override.
    expected_version : str | None
        Optional expected semantic version override.
    kani_command : str
        Command used to query Kani, defaulting to `cargo kani`.

    Returns
    -------
    int
        Exit code, where `0` indicates a matching version.

    Notes
    -----
    `ProverToolError` is handled with `_error_exit`.
    """
    try:
        message = check_kani_version(
            KaniCheckOptions(
                paths=KaniPaths(
                    repo_root=_repo_root(repo_root), version_file=version_file
                ),
                expected_version=expected_version,
                kani_command=kani_command,
            ),
        )
    except ProverToolError as exc:
        return _error_exit(exc)
    _print_lines((message,))
    return 0


@verus_app.command(name="install")
# CLI parameter binding needs one explicit parameter per flag/env-var mapping.
# pylint: disable-next=too-many-arguments
def install_verifier(
    *,
    repo_root: typ.Annotated[Path | None, Parameter(env_var="INPUT_REPO_ROOT")] = None,
    version_file: typ.Annotated[
        Path | None,
        Parameter(env_var=("INPUT_VERSION_FILE", "INPUT_VERUS_VERSION_FILE")),
    ] = None,
    checksum_file: typ.Annotated[
        Path | None,
        Parameter(env_var=("INPUT_CHECKSUM_FILE", "INPUT_VERUS_CHECKSUM_FILE")),
    ] = None,
    target: typ.Annotated[
        str,
        Parameter(env_var=("VERUS_TARGET", "INPUT_VERUS_TARGET")),
    ] = DEFAULT_TARGET,
    install_dir: typ.Annotated[
        Path | None,
        Parameter(env_var=("VERUS_INSTALL_DIR", "INPUT_VERUS_INSTALL_DIR")),
    ] = None,
    base_url: typ.Annotated[
        str, Parameter(env_var="INPUT_VERUS_BASE_URL")
    ] = VERUS_BASE_URL,
) -> int:
    """Install the pinned Verus verifier.

    Parameters
    ----------
    repo_root : Path | None
        Repository root used to resolve default tool pin paths.
    version_file : Path | None
        Optional Verus version pin override.
    checksum_file : Path | None
        Optional Verus checksum file override.
    target : str
        Verus release target, defaulting to `x86-linux`.
    install_dir : Path | None
        Optional installation directory override.
    base_url : str
        Base URL for Verus release downloads.

    Returns
    -------
    int
        Exit code, where `0` indicates success.

    Notes
    -----
    `ProverToolError` is handled with `_error_exit`.
    """
    try:
        lines = install_verus(
            VerusInstallOptions(
                paths=VerusPaths(
                    repo_root=_repo_root(repo_root),
                    version_file=version_file,
                    checksum_file=checksum_file,
                ),
                target=target,
                install_dir=install_dir,
                base_url=base_url,
            ),
        )
    except ProverToolError as exc:
        return _error_exit(exc)
    _print_lines(lines)
    return 0


@verus_app.command(name="run")
# CLI parameter binding needs one explicit parameter per flag/env-var mapping.
# pylint: disable-next=too-many-arguments
def run(
    *,
    repo_root: typ.Annotated[Path | None, Parameter(env_var="INPUT_REPO_ROOT")] = None,
    proof_file: typ.Annotated[
        Path | None,
        Parameter(env_var=("VERUS_PROOF_FILE", "INPUT_PROOF_FILE")),
    ] = None,
    version_file: typ.Annotated[
        Path | None,
        Parameter(env_var=("INPUT_VERSION_FILE", "INPUT_VERUS_VERSION_FILE")),
    ] = None,
    checksum_file: typ.Annotated[
        Path | None,
        Parameter(env_var=("INPUT_CHECKSUM_FILE", "INPUT_VERUS_CHECKSUM_FILE")),
    ] = None,
    install_dir: typ.Annotated[
        Path | None,
        Parameter(env_var=("VERUS_INSTALL_DIR", "INPUT_VERUS_INSTALL_DIR")),
    ] = None,
    verus_bin: typ.Annotated[
        str | None,
        Parameter(env_var=("VERUS_BIN", "INPUT_VERUS_BIN")),
    ] = None,
    ensure_toolchain: typ.Annotated[
        bool,
        Parameter(negative="--no-ensure-toolchain"),
    ] = True,
    install_missing: typ.Annotated[
        bool,
        Parameter(negative="--no-install-missing"),
    ] = True,
    extra_arg: typ.Annotated[
        list[str] | None,
        Parameter(allow_repeating=True, env_var="INPUT_EXTRA_ARG"),
    ] = None,
) -> int:
    """Run Verus against a proof file.

    Parameters
    ----------
    repo_root : Path | None
        Repository root used to resolve default tool pin paths.
    proof_file : Path | None
        Optional proof file override.
    version_file : Path | None
        Optional Verus version pin override.
    checksum_file : Path | None
        Optional checksum file override used if installation is needed.
    install_dir : Path | None
        Optional Verus installation directory.
    verus_bin : str | None
        Optional Verus binary, directory, or command name.
    ensure_toolchain : bool
        Whether to install the required Rust toolchain with `rustup`.
    install_missing : bool
        Whether to install the default Verus binary when missing.
    extra_arg : list[str] | None
        Extra Verus arguments appended after the proof file.

    Returns
    -------
    int
        Exit code, where proof failures preserve the Verus exit code.
    """
    try:
        lines = run_verus(
            VerusRunOptions(
                paths=VerusPaths(
                    repo_root=_repo_root(repo_root),
                    version_file=version_file,
                    checksum_file=checksum_file,
                ),
                proof_file=proof_file,
                install_dir=install_dir,
                verus_bin=verus_bin,
                should_ensure_toolchain=ensure_toolchain,
                should_install_missing=install_missing,
                extra_args=tuple(extra_arg or ()),
            ),
        )
    except ProverToolError as exc:
        return _error_exit(exc)
    _print_lines(lines)
    return 0


app.command(kani_app, name="kani")
app.command(verus_app, name="verus")


def main() -> None:
    """Run the prover tools CLI.

    Raises
    ------
    SystemExit
        Raised by `sys.exit` with the return code from `app()`.

    Notes
    -----
    This is the console-script and `python -m rust_prover_tools.cli` entry
    point. It exits via `sys.exit(app())` rather than returning normally.
    """
    sys.exit(app())


if __name__ == "__main__":
    main()
