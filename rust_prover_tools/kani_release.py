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

from rust_prover_tools.commands import CommandSpec, run_command  # ruff: ignore[unused-import] - qualification's patch point
from rust_prover_tools.errors import CommandFailedError, ProverToolError
from rust_prover_tools.kani_exec_routing import runtime_search_path
from rust_prover_tools.kani_release_config import (
    VerificationConfig,
    backend_overrides,
    check_verification_config,
    checked_resolve,
    checked_stat,
)
from rust_prover_tools.kani_release_flags import verification_flags

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

_RUSTC_IDENTITY = "rustc 1.93.0-nightly (53732d5e0 2025-11-20)"
_TOOLCHAIN_IDENTITY = "nightly-2025-11-21-x86_64-unknown-linux-gnu"
_LOGGER = logging.getLogger(__name__)

#: Kani release this route qualifies.
KANI_RELEASE_VERSION: typ.Final[str] = "0.67.0"

#: Rustc identity the qualified release must report.
RUSTC_IDENTITY: typ.Final[str] = _RUSTC_IDENTITY

#: Toolchain identity the qualified release must report.
TOOLCHAIN_IDENTITY: typ.Final[str] = _TOOLCHAIN_IDENTITY


def _status_class(status: int) -> str:
    """Describe outcomes without including commands or captured user payload."""
    if status == 0:
        return "success"
    return "signal" if status < 0 else "nonzero"


def emit_stage_event(
    stage: str, started: float, fields: cabc.Mapping[str, object]
) -> None:
    """Emit one bounded event for a completed stage, without private payloads.

    Every record on this logger carries the same fields, so an application sink
    can read ``stage`` without inspecting the message. A caller may add counts
    that describe a decision, but never names, paths, arguments, or output.
    """
    _LOGGER.info(
        "Kani execution stage completed",
        extra={
            "stage": stage,
            "release": "0.67.0",
            "duration_seconds": time.monotonic() - started,
            **fields,
        },
    )


def observe_stage[T](
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
        emit_stage_event(
            stage, started, {"status_class": status, "error_category": category}
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


def validate_kani_arguments(arguments: tuple[str, ...]) -> None:
    """Reject setup and routing overrides in a composed verifier vector.

    Parameters
    ----------
    arguments : tuple[str, ...]
        Complete verifier argument vector for one invocation.

    Raises
    ------
    ProverToolError
        Raised when the vector would re-run setup or replace the Cargo
        configuration selection.
    """
    _validate_arguments(arguments)


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


def _apply_backend_overrides(child: dict[str, str], config: VerificationConfig) -> None:
    """Pin every accepted profile backend to LLVM inside the child only.

    Cargo reads `CARGO_PROFILE_<NAME>_CODEGEN_BACKEND` in preference to profile
    tables in configuration files and manifests, so the selected release always
    compiles with its own backend regardless of what the repository asked for.
    """
    started = time.monotonic()
    spellings = {selection.profile for selection in config.selections}
    variables = backend_overrides(config)
    child.update(variables)
    if not config.selections:
        # No profile selected a backend, so there is no decision to report. An
        # event per run would be noise and would make the stage log depend on
        # an unrelated empty repository.
        return
    # Report the decision without naming profiles, paths, or configuration
    # values. A collision means two spellings share one normalized variable;
    # one profile declared in several files is a duplicate, not a collision.
    # The event keeps the stage schema so a sink can read every record alike.
    emit_stage_event(
        "backend_overrides",
        started,
        {
            "status_class": "success",
            "error_category": "none",
            "operation": "backend_overrides",
            "selection_count": len(config.selections),
            "variable_count": len(variables),
            "collision_count": len(spellings) - len(variables),
        },
    )


def _prepare_kani_command(
    options: KaniExecOptions,
    environment: cabc.Mapping[str, str],
    *,
    validate_arguments: bool,
) -> tuple[CommandSpec, Path]:
    """Build the validated command and return its release root."""
    root = checked_resolve(options.installation)
    directory = checked_stat(options.cwd)
    if directory is None or not stat.S_ISDIR(directory.st_mode):
        msg = "Kani exec requires an existing proof working directory"
        raise ProverToolError(msg)
    cwd = checked_resolve(options.cwd)
    _validate_installation(root)
    if validate_arguments:
        _validate_arguments(options.arguments)
    config = check_verification_config(cwd, environment)
    flags = verification_flags(environment)
    child = dict(environment)
    _apply_backend_overrides(child, config)
    child["LD_LIBRARY_PATH"] = runtime_search_path(
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
    spec = CommandSpec(
        (str(root / "bin/kani-driver"), "kani", *options.arguments),
        cwd=cwd,
        env=child,
    )
    return spec, root


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
    return _prepare_kani_command(options, environment, validate_arguments=True)[0]


def prepare_kani_installation(
    options: KaniExecOptions, environment: cabc.Mapping[str, str]
) -> tuple[CommandSpec, Path]:
    """Build a validated command for a supervised per-harness run.

    Parameters
    ----------
    options : KaniExecOptions
        Explicit release installation and proof cwd. The literal argument
        vector may be empty; the supervisor validates each composed vector.
    environment : Mapping[str, str]
        Injected inherited environment; wrapper values are retained verbatim.

    Returns
    -------
    tuple[CommandSpec, Path]
        The validated specification and its release root.
    """
    return _prepare_kani_command(options, environment, validate_arguments=False)


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

    Notes
    -----
    The implementation lives in `rust_prover_tools.kani_qualification`; this
    wrapper keeps the route's established import location.
    """
    from rust_prover_tools.kani_qualification import (
        exec_kani as qualified_exec,
    )

    return qualified_exec(options, environment)
