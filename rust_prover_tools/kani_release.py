"""Explicit, admission-preserving execution of the qualified Kani release.

This route never downloads, installs, or changes the selected installation.
Kani 0.67 on Linux is the sole qualification target; snapshot execution remains
separate from this legacy boundary.
"""

from __future__ import annotations

import dataclasses as dc
import os
import sys
import typing as typ
from pathlib import Path

from rust_prover_tools.commands import CommandSpec, run_command
from rust_prover_tools.errors import CommandFailedError, ProverToolError
from rust_prover_tools.kani_release_config import check_verification_config
from rust_prover_tools.kani_release_flags import verification_flags
from rust_prover_tools.versions import parse_tool_version

if typ.TYPE_CHECKING:
    import collections.abc as cabc

_RUSTC_IDENTITY = "rustc 1.93.0-nightly (53732d5e0 2025-11-20)"
_TOOLCHAIN_IDENTITY = "nightly-2025-11-21-x86_64-unknown-linux-gnu"
_RUSTUP_LIBRARY_SUFFIX_DEPTH = 3


@dc.dataclass(frozen=True, slots=True)
class KaniExecOptions:
    """An explicit installed release, proof cwd, and literal verifier argv."""

    installation: Path
    cwd: Path
    arguments: tuple[str, ...]


def _validate_installation(root: Path) -> None:
    """Validate the measured ABI metadata and required runtime assets."""
    if sys.platform != "linux":
        msg = "Kani exec currently qualifies only the Linux 0.67.0 release"
        raise ProverToolError(msg)
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
    for relative in (
        "bin/kani-driver",
        "bin/kani-compiler",
        "toolchain/bin/rustc",
        "toolchain/bin/cargo",
    ):
        path = root / relative
        if not path.is_file() or not os.access(path, os.X_OK):
            msg = f"Kani release executable is missing or not executable: {path}"
            raise ProverToolError(msg)
    driver = root / "bin/kani-driver"
    if driver.resolve() != driver:
        msg = "Kani driver must reside in the selected installation bin directory"
        raise ProverToolError(msg)
    runtime = root / "toolchain/lib/libLLVM.so.21.1-rust-1.93.0-nightly"
    if not runtime.is_file():
        msg = "Kani release matching LLVM runtime is missing"
        raise ProverToolError(msg)


def _validate_arguments(arguments: tuple[str, ...]) -> None:
    """Reject routes that could install or change the checked Cargo context."""
    forbidden = ("--config", "--profile", "--manifest-path")
    has_forbidden_route = "setup" in arguments or any(
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
    root = options.installation.resolve()
    cwd = options.cwd.resolve()
    _validate_installation(root)
    _validate_arguments(options.arguments)
    check_verification_config(cwd, environment)
    flags = verification_flags(environment)
    child = dict(environment)
    child["LD_LIBRARY_PATH"] = _runtime_search_path(
        root / "toolchain/lib", child.get("LD_LIBRARY_PATH", "")
    )
    child["RUSTFLAGS"] = " ".join(flags)
    # Cuprum overlays rather than replaces the environment. Both spellings
    # carry equivalent sanitized flags; Kani 0.67 synthesizes encoded flags
    # from the plain spelling before launching its bundled Cargo.
    child["CARGO_ENCODED_RUSTFLAGS"] = "\x1f".join(flags)
    child["PATH"] = os.pathsep.join((
        str(root / "toolchain/bin"),
        str(root / "bin"),
        str(root / "pyroot/bin"),
        child.get("PATH", ""),
    ))
    child["PYTHONPATH"] = os.pathsep.join((
        str(root / "pyroot"),
        child.get("PYTHONPATH", ""),
    ))
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
    spec = prepare_kani_command(options, environment)
    root = options.installation.resolve()
    chain = tuple(
        environment[variable]
        for variable in ("RUSTC_WRAPPER", "RUSTC_WORKSPACE_WRAPPER")
        if variable in environment
    )
    for relative in ("toolchain/bin/rustc", "bin/kani-compiler"):
        _checked_identity(
            spec, (*chain, str(root / relative), "--version"), _RUSTC_IDENTITY
        )
    version = run_command(
        CommandSpec((spec.argv[0], "kani", "--version"), cwd=spec.cwd, env=spec.env)
    )
    if version.exit_code != 0:
        raise CommandFailedError(spec.argv[0], version.exit_code, version.stderr)
    if (
        parse_tool_version(version.stdout + version.stderr, tool_name="Kani")
        != "0.67.0"
    ):
        msg = "Kani selected verifier identity is outside the qualified 0.67.0 release"
        raise ProverToolError(msg)
    return run_command(dc.replace(spec, echo=True)).exit_code
