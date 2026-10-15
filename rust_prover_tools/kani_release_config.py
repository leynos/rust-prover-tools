"""Fail-closed Cargo configuration checks for explicit legacy verification."""

from __future__ import annotations

import tomllib
import typing as typ
from pathlib import Path

from rust_prover_tools.errors import ProverToolError

if typ.TYPE_CHECKING:
    import collections.abc as cabc

_TOOL_ROUTES = (
    ("rustc-wrapper", "RUSTC_WRAPPER"),
    ("rustc-workspace-wrapper", "RUSTC_WORKSPACE_WRAPPER"),
    ("rustdoc", "RUSTDOC"),
)


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


def _check_document(document: object, environment: cabc.Mapping[str, str]) -> None:
    """Reject configuration whose independent backend cannot be isolated."""
    if not isinstance(document, dict):
        return
    _check_routing_overrides(document, environment)
    for key, value in document.items():
        if key in {"codegen-backend", "include"}:
            msg = (
                "Kani exec cannot prove isolation of Cargo backend/include "
                "configuration"
            )
            raise ProverToolError(msg)
        if isinstance(value, dict):
            _check_document(value, environment)


def _check_file(path: Path, environment: cabc.Mapping[str, str]) -> None:
    """Validate one discovered file without resolving an absent file as safe."""
    if not path.exists():
        return
    try:
        _check_document(tomllib.loads(path.read_text(encoding="utf-8")), environment)
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        msg = f"Kani exec could not validate Cargo configuration at {path}"
        raise ProverToolError(msg) from exc


def _check_config_directory(
    directory: Path, environment: cabc.Mapping[str, str]
) -> None:
    """Refuse ambiguous legacy/modern files before reading a Cargo directory."""
    paths = (directory / "config", directory / "config.toml")
    if all(path.exists() for path in paths):
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
    cargo_home = (
        Path(home)
        if home
        else Path(environment.get("HOME", str(Path.home()))) / ".cargo"
    )
    if not cargo_home.is_absolute():
        cargo_home = cwd / cargo_home
    directories = (cwd, *cwd.parents) if search_roots is None else search_roots
    for directory in directories:
        _check_config_directory(directory / ".cargo", environment)
        _check_file(directory / "Cargo.toml", environment)
    _check_config_directory(cargo_home, environment)
