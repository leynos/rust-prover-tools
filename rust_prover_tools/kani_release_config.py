"""Validate Cargo inputs used by the explicit Kani release workflow.

Configuration routing is checked separately from manifest profile policy so
publication metadata cannot masquerade as Cargo configuration. Filesystem
queries fail closed and report expected failures through ProverToolError.
"""

from __future__ import annotations

import errno
import tomllib
import typing as typ
from pathlib import Path

from rust_prover_tools.errors import ProverToolError

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from os import stat_result

_TOOL_ROUTES = (
    ("rustc-wrapper", "RUSTC_WRAPPER"),
    ("rustc-workspace-wrapper", "RUSTC_WORKSPACE_WRAPPER"),
    ("rustdoc", "RUSTDOC"),
)


def checked_stat(path: Path) -> stat_result | None:
    """Treat only missing path components as absence, preserving other failures."""
    try:
        return path.stat()
    except OSError as exc:
        if exc.errno in {errno.ENOENT, errno.ENOTDIR}:
            return None
        msg = f"Kani exec could not inspect filesystem input at {path}"
        raise ProverToolError(msg) from exc
    except (UnicodeError, ValueError) as exc:
        msg = "Kani exec could not represent a filesystem input"
        raise ProverToolError(msg) from exc


def checked_resolve(path: Path) -> Path:
    """Resolve an existing input without masking permission or symlink failures."""
    try:
        return path.resolve(strict=True)
    except (OSError, UnicodeError, ValueError) as exc:
        msg = f"Kani exec could not resolve filesystem input at {path}"
        raise ProverToolError(msg) from exc


def _has_selected_tool(environment: cabc.Mapping[str, str], variable: str) -> bool:
    """Require a representable non-empty selection before relying on precedence."""
    value = environment.get(variable)
    if not value or "\x00" in value:
        return False
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeError:
        return False
    return True


def _check_process_tool_selections(environment: cabc.Mapping[str, str]) -> None:
    """Allow absent tools, preserving valid selections and refusing hidden routes."""
    for key, variable in _TOOL_ROUTES:
        if variable in environment and not _has_selected_tool(environment, variable):
            msg = f"Kani exec refuses invalid process tool selection for {variable}"
            raise ProverToolError(msg)
        if "CARGO_BUILD_" + variable in environment and not _has_selected_tool(
            environment, variable
        ):
            msg = f"Kani exec refuses unproved Cargo compiler routing via {key}"
            raise ProverToolError(msg)


def _check_routing_overrides(
    document: dict[str, object], environment: cabc.Mapping[str, str]
) -> None:
    """Reject unseen compiler routing without rejecting overridden defaults."""
    protected = {
        "RUSTC_WRAPPER",
        "RUSTC_WORKSPACE_WRAPPER",
        "RUSTDOC",
        "RUSTFLAGS",
        "CARGO_ENCODED_RUSTFLAGS",
        "LD_LIBRARY_PATH",
        "LD_PRELOAD",
    }
    protected.update("CARGO_BUILD_" + variable for _, variable in _TOOL_ROUTES)
    variables = document.get("env")
    if isinstance(variables, dict) and protected.intersection(variables):
        msg = "Kani exec refuses Cargo env overrides of verification routing"
        raise ProverToolError(msg)
    build = document.get("build")
    if not isinstance(build, dict):
        return
    for key, variable in _TOOL_ROUTES:
        # Bundled Cargo context::maybe_get_tool selects a UTF-8 process value
        # before the [build] fallback. Preserve that selection verbatim.
        if key in build and not _has_selected_tool(environment, variable):
            msg = f"Kani exec refuses unproved Cargo compiler routing via {key}"
            raise ProverToolError(msg)


def _check_backend(document: object) -> None:
    """Reject independently selected backends within a Cargo control table."""
    if not isinstance(document, dict):
        return
    for key, value in document.items():
        if key == "codegen-backend":
            msg = "Kani exec cannot prove isolation of Cargo backend configuration"
            raise ProverToolError(msg)
        if isinstance(value, dict):
            _check_backend(value)


def _check_document(
    document: dict[str, object],
    environment: cabc.Mapping[str, str],
    *,
    is_manifest: bool,
) -> None:
    """Keep manifest metadata distinct from Cargo's executable control tables."""
    if is_manifest:
        _check_backend(document.get("profile"))
        return
    if "include" in document:
        msg = "Kani exec cannot prove isolation of Cargo include configuration"
        raise ProverToolError(msg)
    _check_routing_overrides(document, environment)
    _check_backend(document)


def _check_file(
    path: Path,
    environment: cabc.Mapping[str, str],
    *,
    is_manifest: bool = False,
) -> None:
    """Validate one discovered file without resolving an absent file as safe."""
    if checked_stat(path) is None:
        return
    try:
        _check_document(
            tomllib.loads(path.read_text(encoding="utf-8")),
            environment,
            is_manifest=is_manifest,
        )
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        msg = f"Kani exec could not validate Cargo configuration at {path}"
        raise ProverToolError(msg) from exc


def _check_config_directory(
    directory: Path, environment: cabc.Mapping[str, str]
) -> None:
    """Refuse ambiguous legacy/modern files before reading a Cargo directory."""
    paths = (directory / "config", directory / "config.toml")
    if all(checked_stat(path) is not None for path in paths):
        msg = "Kani exec refuses ambiguous config and config.toml files"
        raise ProverToolError(msg)
    for path in paths:
        _check_file(path, environment)


def check_verification_config(
    cwd: Path,
    environment: cabc.Mapping[str, str],
    *,
    search_roots: tuple[Path, ...] | None = None,
) -> None:
    """Validate discovered Cargo config and ancestor profile manifests.

    Parameters
    ----------
    cwd : Path
        Actual verifier working directory, not a dependency's source root.
    environment : Mapping[str, str]
        Injected environment supplying HOME and optional CARGO_HOME.
    search_roots : tuple[Path, ...] | None
        Internal pure-test discovery boundary. Production callers omit it and
        always discover the actual working directory and every ancestor.

    Raises
    ------
    ProverToolError
        A malformed tool selection, backend, unresolved include, ambiguous
        config pair, or unreadable configuration prevents verification isolation.
    """
    _check_process_tool_selections(environment)
    home = environment.get("CARGO_HOME")
    if home:
        cargo_home = Path(home)
    elif environment.get("HOME"):
        cargo_home = Path(environment["HOME"]) / ".cargo"
    else:
        msg = "Kani exec requires injected HOME or CARGO_HOME for Cargo discovery"
        raise ProverToolError(msg)
    if not cargo_home.is_absolute():
        cargo_home = cwd / cargo_home
    directories = (cwd, *cwd.parents) if search_roots is None else search_roots
    for directory in directories:
        _check_config_directory(directory / ".cargo", environment)
        _check_file(directory / "Cargo.toml", environment, is_manifest=True)
    _check_config_directory(cargo_home, environment)
