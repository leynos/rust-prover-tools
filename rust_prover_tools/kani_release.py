"""Explicit, admission-preserving execution of the qualified Kani release.

This route never downloads, installs, or changes the selected installation.
Kani 0.67 on Linux is the sole qualification target; snapshot execution remains
separate from this legacy boundary. Cargo configuration and compiler flags are
validated by the feature helpers before Cuprum runs the selected release.
"""

from __future__ import annotations

import dataclasses as dc
import logging
import os
import platform
import stat
import sys
import time
import typing as typ
from functools import partial
from pathlib import Path

from rust_prover_tools.commands import CommandSpec, run_command
from rust_prover_tools.errors import CommandFailedError, ProverToolError
from rust_prover_tools.kani_release_config import (
    VerificationConfig,
    check_verification_config,
    checked_resolve,
    checked_stat,
    codegen_backend_variable,
)
from rust_prover_tools.kani_release_flags import verification_flags

if typ.TYPE_CHECKING:
    import collections.abc as cabc

_RUSTC_IDENTITY = "rustc 1.93.0-nightly (53732d5e0 2025-11-20)"
_TOOLCHAIN_IDENTITY = "nightly-2025-11-21-x86_64-unknown-linux-gnu"
_RUSTUP_LIBRARY_SUFFIX_DEPTH = 3
_LOGGER = logging.getLogger(__name__)


def _status_class(status: int) -> str:
    """Describe outcomes without including commands or captured user payload."""
    if status == 0:
        return "success"
    return "signal" if status < 0 else "nonzero"


def _observe_stage[T](
    stage: str,
    operation: cabc.Callable[[], T],
    *,
    status_of: cabc.Callable[[T], int] | None = None,
) -> T:
    """Emit one opt-in structured event for a boundary stage, without payloads."""
    started = time.monotonic()
    status, category = "failure", "unexpected"
    try:
        result = operation()
    except CommandFailedError as exc:
        status, category = _status_class(exc.exit_code), "command"
        raise
    except ProverToolError:
        category = "validation"
        raise
    else:
        status = _status_class(status_of(result) if status_of else 0)
        category = "none" if status == "success" else "command"
        return result
    finally:
        # INFO keeps the normal CLI output unchanged unless the application
        # explicitly enables logging. Never attach exception text or traceback.
        _LOGGER.info(
            "Kani execution stage completed",
            extra={
                "stage": stage,
                "release": "0.67.0",
                "duration_seconds": time.monotonic() - started,
                "status_class": status,
                "error_category": category,
            },
        )


@dc.dataclass(frozen=True, slots=True)
class KaniExecOptions:
    """An explicit installed release, proof cwd, and literal verifier argv."""

    installation: Path
    cwd: Path
    arguments: tuple[str, ...]


def _check_release_metadata(root: Path) -> None:
    """Require the declared compiler and toolchain to match the measured ABI."""
    try:
        identity = (root / "rustc-version").read_text(encoding="utf-8").strip()
        toolchain = (
            (root / "rust-toolchain-version").read_text(encoding="utf-8").strip()
        )
    except (OSError, UnicodeError) as exc:
        msg = "Kani exec requires a complete, explicitly selected release installation"
        raise ProverToolError(msg) from exc
    if (identity, toolchain) != (_RUSTC_IDENTITY, _TOOLCHAIN_IDENTITY):
        msg = (
            "Kani release compiler identity is outside the qualified "
            "0.67.0 installation"
        )
        raise ProverToolError(msg)


def _is_executable(path: Path) -> bool:
    """Distinguish missing/non-regular assets before checking execute access."""
    metadata = checked_stat(path)
    if metadata is None:
        return False
    return stat.S_ISREG(metadata.st_mode) and os.access(path, os.X_OK)


def _validate_installation(root: Path) -> None:
    """Validate the measured ABI metadata and required runtime assets."""
    if sys.platform != "linux" or platform.machine() != "x86_64":
        msg = "Kani exec currently qualifies only the Linux x86_64 0.67.0 release"
        raise ProverToolError(msg)
    _check_release_metadata(root)
    for relative in (
        "bin/kani-driver",
        "bin/kani-compiler",
        "toolchain/bin/rustc",
        "toolchain/bin/cargo",
    ):
        path = root / relative
        if not _is_executable(path):
            msg = f"Kani release executable is missing or not executable: {path}"
            raise ProverToolError(msg)
    driver = root / "bin/kani-driver"
    if checked_resolve(driver) != driver:
        msg = "Kani driver must reside in the selected installation bin directory"
        raise ProverToolError(msg)
    toolchain = checked_resolve(root / "toolchain")
    for executable in ("cargo", "rustc"):
        resolved = checked_resolve(root / "toolchain/bin" / executable)
        if resolved.parent != toolchain / "bin":
            msg = "Kani executable must remain in the selected toolchain bin directory"
            raise ProverToolError(msg)
    runtime = root / "toolchain/lib/libLLVM.so.21.1-rust-1.93.0-nightly"
    metadata = checked_stat(runtime)
    if metadata is None or not stat.S_ISREG(metadata.st_mode):
        msg = "Kani release matching LLVM runtime is missing"
        raise ProverToolError(msg)


def _validate_arguments(arguments: tuple[str, ...]) -> None:
    """Reject routes that could install or change the checked Cargo context."""
    forbidden = ("--config", "--profile", "--manifest-path")
    has_forbidden_route = arguments[:1] == ("setup",) or any(
        arg.split("=", 1)[0] in forbidden for arg in arguments
    )
    if not arguments or has_forbidden_route:
        msg = (
            "Kani exec needs verifier arguments without setup, --config, "
            "--profile, or --manifest-path"
        )
        raise ProverToolError(msg)


def _runtime_search_path(runtime: Path, inherited: str) -> str:
    """Retain release-proxy hygiene while adding the matching runtime first."""

    def compatible(entry: str) -> bool:
        parts = Path(entry).parts
        return not (
            len(parts) >= _RUSTUP_LIBRARY_SUFFIX_DEPTH
            and parts[-1] == "lib"
            and parts[-_RUSTUP_LIBRARY_SUFFIX_DEPTH] == "toolchains"
        )

    entries = (
        tuple(entry for entry in inherited.split(os.pathsep) if compatible(entry))
        if inherited
        else ()
    )
    return os.pathsep.join((str(runtime), *entries))


def _apply_backend_overrides(child: dict[str, str], config: VerificationConfig) -> None:
    """Pin every accepted profile backend to LLVM inside the child only.

    Cargo reads `CARGO_PROFILE_<NAME>_CODEGEN_BACKEND` in preference to profile
    tables in configuration files and manifests, so the selected release always
    compiles with its own backend regardless of what the repository asked for.
    """
    for selection in config.selections:
        child[codegen_backend_variable(selection.profile)] = "llvm"


def prepare_kani_command(
    options: KaniExecOptions, environment: cabc.Mapping[str, str]
) -> CommandSpec:
    """Construct a child-scoped verifier command after routing validation.

    Parameters
    ----------
    options : KaniExecOptions
        Explicit release installation, proof cwd, and literal arguments.
    environment : Mapping[str, str]
        Injected inherited environment; wrapper values are retained verbatim.

    Returns
    -------
    CommandSpec
        Validated Cuprum specification with normalized verification flags.

    Raises
    ------
    ProverToolError
        Installation, arguments, or configuration cannot be qualified safely.
    """
    root = checked_resolve(options.installation)
    directory = checked_stat(options.cwd)
    if directory is None or not stat.S_ISDIR(directory.st_mode):
        msg = "Kani exec requires an existing proof working directory"
        raise ProverToolError(msg)
    cwd = checked_resolve(options.cwd)
    _validate_installation(root)
    _validate_arguments(options.arguments)
    config = check_verification_config(cwd, environment)
    flags = verification_flags(environment)
    child = dict(environment)
    _apply_backend_overrides(child, config)
    child["LD_LIBRARY_PATH"] = _runtime_search_path(
        root / "toolchain/lib", child.get("LD_LIBRARY_PATH", "")
    )
    child["RUSTFLAGS"] = " ".join(flags)
    # Cuprum overlays rather than replaces the environment. Both spellings
    # carry equivalent sanitized flags; Kani 0.67 synthesizes encoded flags
    # from the plain spelling before launching its bundled Cargo.
    child["CARGO_ENCODED_RUSTFLAGS"] = "\x1f".join(flags)
    path_prefix = (
        str(root / "toolchain/bin"),
        str(root / "bin"),
        str(root / "pyroot/bin"),
    )
    for variable, prefix in (
        ("PATH", path_prefix),
        ("PYTHONPATH", (str(root / "pyroot"),)),
    ):
        inherited = child.get(variable)
        child[variable] = os.pathsep.join((*prefix, inherited) if inherited else prefix)
    child["RUSTUP_TOOLCHAIN"] = _TOOLCHAIN_IDENTITY
    return CommandSpec(
        (str(root / "bin/kani-driver"), "kani", *options.arguments), cwd=cwd, env=child
    )


def _checked_identity(spec: CommandSpec, argv: tuple[str, ...], expected: str) -> None:
    """Query a selected tool without masking nonzero status or identity drift."""
    result = run_command(CommandSpec(argv, cwd=spec.cwd, env=spec.env))
    if result.exit_code != 0:
        raise CommandFailedError(argv[0], result.exit_code, result.stderr)
    if result.stdout.strip() != expected:
        msg = (
            "Kani selected binary identity does not match the qualified "
            "release metadata"
        )
        raise ProverToolError(msg)


def _checked_verifier(spec: CommandSpec) -> None:
    """Accept only the qualified stable driver's complete version response."""
    version = run_command(
        CommandSpec((spec.argv[0], "kani", "--version"), cwd=spec.cwd, env=spec.env)
    )
    if version.exit_code != 0:
        raise CommandFailedError(spec.argv[0], version.exit_code, version.stderr)
    if (version.stdout + version.stderr).strip() not in {
        "kani 0.67.0",
        "cargo-kani 0.67.0",
    }:
        msg = "Kani selected verifier identity is outside the qualified 0.67.0 release"
        raise ProverToolError(msg)


def exec_kani(options: KaniExecOptions, environment: cabc.Mapping[str, str]) -> int:
    """Validate identities and stream a legacy release invocation through Cuprum.

    Parameters
    ----------
    options : KaniExecOptions
        Explicit release and proof invocation.
    environment : Mapping[str, str]
        Injected inherited environment, including admission wrappers.

    Returns
    -------
    int
        Child status, including negative signal status, without success masking.

    Raises
    ------
    ProverToolError
        The release identity or verification route could not be qualified.
    CommandFailedError
        An identity query failed before proof execution.
    """
    spec = _observe_stage(
        "validation", partial(prepare_kani_command, options, environment)
    )
    root = checked_resolve(options.installation)
    chain = tuple(
        environment[variable]
        for variable in ("RUSTC_WRAPPER", "RUSTC_WORKSPACE_WRAPPER")
        if variable in environment
    )
    for stage, relative in (
        ("rustc_identity", "toolchain/bin/rustc"),
        ("compiler_identity", "bin/kani-compiler"),
    ):
        _observe_stage(
            stage,
            partial(
                _checked_identity,
                spec,
                (*chain, str(root / relative), "--version"),
                _RUSTC_IDENTITY,
            ),
        )
    _observe_stage("verifier_identity", partial(_checked_verifier, spec))
    return _observe_stage(
        "proof",
        partial(run_command, dc.replace(spec, echo=True)),
        status_of=lambda result: result.exit_code,
    ).exit_code
