"""Adapt public Verus run options to the proof-run workflow.

The command lives beside `cli.py` for the same reason as `kani_exec_cli.py`:
its parameter table is long enough that keeping it inline would crowd the
application module. Exit-code mapping stays local so the adapter reports the
verifier's own status without a shared helper it cannot own.
"""

from __future__ import annotations

import sys
import typing as typ
from pathlib import Path

from cyclopts import Parameter

from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.exit_status import print_lines
from rust_prover_tools.verus import (
    DEFAULT_TARGET,
    VerusPaths,
    VerusProofFailedError,
    VerusRunOptions,
    run_verus,
)


def _single_env_token(
    _type: object,
    value: str,
    *,
    delimiter: str | None = None,
) -> list[str]:
    """Return one environment value as a single token.

    Cyclopts splits iterable `Path` values on `os.pathsep`. The legacy
    `VERUS_PROOF_FILE` and `INPUT_PROOF_FILE` bindings each supply exactly one
    proof file, so a separator inside a path must not invent extra files.
    """
    del delimiter
    return [value]


# CLI parameter binding needs one explicit parameter per flag/env-var mapping.
# pylint: disable-next=too-many-arguments
def verus_run(  # ruff: ignore[too-many-arguments] - one parameter per CLI binding
    *,
    repo_root: typ.Annotated[Path | None, Parameter(env_var="INPUT_REPO_ROOT")] = None,
    proof_file: typ.Annotated[
        tuple[Path, ...] | None,
        Parameter(
            allow_repeating=True,
            env_var=("VERUS_PROOF_FILE", "INPUT_PROOF_FILE"),
            env_var_split=_single_env_token,
        ),
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
    target: typ.Annotated[
        str,
        Parameter(env_var=("VERUS_TARGET", "INPUT_VERUS_TARGET")),
    ] = DEFAULT_TARGET,
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
    """Run Verus against one or more proof files.

    Parameters
    ----------
    repo_root : Path | None
        Repository root used to resolve default tool pin paths.
    proof_file : tuple[Path, ...] | None
        Proof files to verify, in the order given. Repeat the flag to name
        more than one file; when omitted, the compatibility example path is
        used.
    version_file : Path | None
        Optional Verus version pin override.
    checksum_file : Path | None
        Optional checksum file override used if installation is needed.
    install_dir : Path | None
        Optional Verus installation directory.
    target : str
        Verus release target used if installation is needed.
    verus_bin : str | None
        Optional Verus binary, directory, or command name.
    ensure_toolchain : bool
        Whether to install the required Rust toolchain with `rustup`.
    install_missing : bool
        Whether to install the default Verus binary when missing.
    extra_arg : list[str] | None
        Extra Verus arguments appended after each proof file.

    Returns
    -------
    int
        Exit code, where proof failures preserve the Verus exit code.

    Notes
    -----
    Verus accepts one input filename per invocation, so each proof file runs
    as its own verifier invocation. See `run_verus` for ordering, partial
    result reporting, and first-failure behaviour.
    """
    try:
        lines = run_verus(
            VerusRunOptions(
                paths=VerusPaths(
                    repo_root=(repo_root or Path.cwd()).resolve(),
                    version_file=version_file,
                    checksum_file=checksum_file,
                ),
                proof_files=tuple(proof_file or ()),
                install_dir=install_dir,
                target=target,
                verus_bin=verus_bin,
                should_ensure_toolchain=ensure_toolchain,
                should_install_missing=install_missing,
                extra_args=tuple(extra_arg or ()),
            ),
        )
    except ProverToolError as exc:
        # A closed pipe loses only this diagnostic; the proof result must survive.
        print_lines((str(exc),), stream=sys.stderr)
        match exc:
            case VerusProofFailedError(exit_code=code):
                return code
            case _:
                return 1
    print_lines(lines)
    return 0
