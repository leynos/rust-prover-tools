"""Validate Cargo inputs used by the explicit Kani release workflow.

Configuration routing is checked separately from manifest profile policy so
publication metadata cannot masquerade as Cargo configuration. Filesystem
queries fail closed and report expected failures through ProverToolError.

Profile-level `codegen-backend` selections are accepted because Cargo gives
`CARGO_PROFILE_<NAME>_CODEGEN_BACKEND` precedence over both configuration files
and manifests. Every other position is refused: per-package overrides were
measured to ignore that variable, and an unresolved `include` hides inputs.
"""

from __future__ import annotations

import dataclasses as dc
import errno
import re
import tomllib
import typing as typ
from pathlib import Path

from rust_prover_tools.errors import ProverToolError

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from os import stat_result

_PROFILE_NAME = re.compile(r"[A-Za-z0-9_-]+")
# Keys that reach the unstable enable flag, including the flag key itself.
_UNSTABLE_ENABLE_FLAG = ("unstable", "codegen-backend")
# Keys that reach a profile-level selection: `profile.<name>.codegen-backend`.
_PROFILE_SELECTION = ("profile", "<name>", "codegen-backend")
_UNPROVED_BACKEND = "Kani exec cannot prove isolation of Cargo backend configuration"
_PACKAGE_BACKEND = (
    "Kani exec cannot prove isolation of a package-specific Cargo backend override"
)

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


@dc.dataclass(frozen=True, slots=True)
class BackendSelection:
    """One Cargo profile backend that the child environment can override."""

    source: Path
    profile: str
    backend: str


@dc.dataclass(frozen=True, slots=True)
class VerificationConfig:
    """Accepted profile backends discovered across Cargo's configuration roots."""

    selections: tuple[BackendSelection, ...] = ()


def codegen_backend_variable(profile: str) -> str:
    """Name Cargo's environment key for one profile-level backend selection.

    Parameters
    ----------
    profile : str
        Cargo profile name as written in configuration or a manifest.

    Returns
    -------
    str
        The `CARGO_PROFILE_<NAME>_CODEGEN_BACKEND` variable Cargo reads, which
        uppercases the profile and maps dashes to underscores.
    """
    normalized = profile.upper().replace("-", "_")
    return f"CARGO_PROFILE_{normalized}_CODEGEN_BACKEND"


def _profile_name_is_overridable(profile: object) -> bool:
    """Require a profile name whose environment variable has no ambiguity."""
    return isinstance(profile, str) and _PROFILE_NAME.fullmatch(profile) is not None


def _is_unstable_enable_flag(keys: tuple[str, ...], value: object) -> bool:
    """Recognize the flag that only unlocks the unstable profile option."""
    return keys == _UNSTABLE_ENABLE_FLAG and isinstance(value, bool)


def _is_profile_selection(keys: tuple[str, ...]) -> bool:
    """Recognize `profile.<name>.codegen-backend`, the overridable position."""
    return keys[:1] == ("profile",) and len(keys) == len(_PROFILE_SELECTION)


def _is_package_selection(keys: tuple[str, ...]) -> bool:
    """Recognize `profile.<name>.package.<package>.codegen-backend` and deeper."""
    return keys[:1] == ("profile",) and keys[2:3] == ("package",)


def _collect_profile_selection(
    path: Path,
    table: tuple[str, ...],
    value: object,
    selections: list[BackendSelection],
) -> None:
    """Record an overridable selection or refuse the position and its value."""
    profile = table[1]
    if not _profile_name_is_overridable(profile):
        msg = (
            f"{_UNPROVED_BACKEND}: {path} names an unusual profile "
            f"{profile!r}, whose environment variable is undefined"
        )
        raise ProverToolError(msg)
    if not isinstance(value, str) or not value:
        msg = (
            f"{_UNPROVED_BACKEND}: {path} sets "
            f"[{'.'.join((*table, 'codegen-backend'))}], which "
            "does not name a backend"
        )
        raise ProverToolError(msg)
    selections.append(BackendSelection(path, profile, value))


def _check_backend(
    path: Path,
    table: tuple[str, ...],
    document: object,
    selections: list[BackendSelection],
) -> None:
    """Classify every `codegen-backend` key by the position that names it.

    Only the boolean `[unstable]` enable flag and a string `[profile.<name>]`
    selection are accepted. Lists are never walked because Cargo reads none of
    their entries as a backend selection.
    """
    if not isinstance(document, dict):
        return
    for key, value in document.items():
        here = (*table, key)
        if key == "codegen-backend":
            if _is_unstable_enable_flag(here, value):
                continue
            if _is_profile_selection(here):
                _collect_profile_selection(path, table, value, selections)
                continue
            if _is_package_selection(here):
                # The measured child environment cannot shadow a package
                # override, so this position always fails closed.
                msg = (
                    f"{_PACKAGE_BACKEND}: {path} sets [{'.'.join(here)}], which "
                    f"{codegen_backend_variable(table[1])} cannot override"
                )
                raise ProverToolError(msg)
            msg = (
                f"{_UNPROVED_BACKEND}: {path} sets [{'.'.join(here)}], which "
                "the configured environment cannot override"
            )
            raise ProverToolError(msg)
        if isinstance(value, dict):
            _check_backend(path, here, value, selections)


def _check_document(
    path: Path,
    document: dict[str, object],
    environment: cabc.Mapping[str, str],
    *,
    is_manifest: bool,
) -> tuple[BackendSelection, ...]:
    """Keep manifest metadata distinct from Cargo's executable control tables."""
    selections: list[BackendSelection] = []
    if is_manifest:
        # Only the workspace-root [profile] table selects a manifest backend;
        # every other manifest key is publication metadata.
        profile = document.get("profile")
        _check_backend(path, ("profile",), profile, selections)
        return tuple(selections)
    if "include" in document:
        msg = "Kani exec cannot prove isolation of Cargo include configuration"
        raise ProverToolError(msg)
    _check_routing_overrides(document, environment)
    _check_backend(path, (), document, selections)
    return tuple(selections)


def _check_file(
    path: Path,
    environment: cabc.Mapping[str, str],
    *,
    is_manifest: bool = False,
) -> tuple[BackendSelection, ...]:
    """Validate one discovered file without resolving an absent file as safe."""
    if checked_stat(path) is None:
        return ()
    try:
        return _check_document(
            path,
            tomllib.loads(path.read_text(encoding="utf-8")),
            environment,
            is_manifest=is_manifest,
        )
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        msg = f"Kani exec could not validate Cargo configuration at {path}"
        raise ProverToolError(msg) from exc


def _check_config_directory(
    directory: Path, environment: cabc.Mapping[str, str]
) -> tuple[BackendSelection, ...]:
    """Refuse ambiguous legacy/modern files before reading a Cargo directory."""
    paths = (directory / "config", directory / "config.toml")
    if all(checked_stat(path) is not None for path in paths):
        msg = "Kani exec refuses ambiguous config and config.toml files"
        raise ProverToolError(msg)
    selections: list[BackendSelection] = []
    for path in paths:
        selections.extend(_check_file(path, environment))
    return tuple(selections)


def check_verification_config(
    cwd: Path,
    environment: cabc.Mapping[str, str],
    *,
    search_roots: tuple[Path, ...] | None = None,
) -> VerificationConfig:
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

    Returns
    -------
    VerificationConfig
        Accepted profile-level backend selections in discovery order. Callers
        must shadow every one in the child environment before execution.

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
    selections: list[BackendSelection] = []
    for directory in directories:
        selections.extend(_check_config_directory(directory / ".cargo", environment))
        selections.extend(
            _check_file(directory / "Cargo.toml", environment, is_manifest=True)
        )
    selections.extend(_check_config_directory(cargo_home, environment))
    return VerificationConfig(selections=tuple(selections))
