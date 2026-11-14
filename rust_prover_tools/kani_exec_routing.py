"""Compiler admission routes shared by Kani release and snapshot execution.

Both execution paths must reach the compiler through the same configured
admission wrappers and must load libraries only from the selected toolchain.
Keeping those rules in one module stops the two paths from disagreeing about
which compiler the user actually configured.
"""

from __future__ import annotations

import os
import typing as typ
from pathlib import Path

from rust_prover_tools.errors import ProverToolError

if typ.TYPE_CHECKING:
    import collections.abc as cabc

__all__ = ("admission_chain", "check_process_tool_selections", "runtime_search_path")

#: Configuration keys that select a compiler and the process variable that
#: Cargo reads in preference to them.
TOOL_ROUTES: typ.Final[tuple[tuple[str, str], ...]] = (
    ("rustc-wrapper", "RUSTC_WRAPPER"),
    ("rustc-workspace-wrapper", "RUSTC_WORKSPACE_WRAPPER"),
    ("rustdoc", "RUSTDOC"),
)

#: `toolchains/<name>/lib` has three trailing components, so a shorter path
#: cannot name a Rustup toolchain library directory.
_RUSTUP_LIBRARY_SUFFIX_DEPTH: typ.Final[int] = 3


def has_selected_tool(environment: cabc.Mapping[str, str], variable: str) -> bool:
    """Return whether a process tool variable names an invokable program."""
    value = environment.get(variable)
    if not value or "\x00" in value:
        return False
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeError:
        return False
    return True


def check_process_tool_selections(environment: cabc.Mapping[str, str]) -> None:
    """Allow absent tools, preserving valid selections and refusing hidden routes.

    Parameters
    ----------
    environment : Mapping[str, str]
        Injected environment holding the configured routes.

    Raises
    ------
    ProverToolError
        A selected route is empty or unrepresentable, or a Cargo variable
        claims a route its process variable does not prove.
    """
    for key, variable in TOOL_ROUTES:
        if variable in environment and not has_selected_tool(environment, variable):
            msg = f"Kani exec refuses invalid process tool selection for {variable}"
            raise ProverToolError(msg)
        if "CARGO_BUILD_" + variable in environment and not has_selected_tool(
            environment, variable
        ):
            msg = f"Kani exec refuses unproved Cargo compiler routing via {key}"
            raise ProverToolError(msg)


def admission_chain(environment: cabc.Mapping[str, str]) -> tuple[str, ...]:
    """Return the configured compiler admission wrappers, outermost first.

    A configured wrapper is a supported route: the release and snapshot
    runners execute compilers through it rather than around it. An empty,
    non-UTF-8 or NUL-containing selection cannot be invoked and is refused
    instead of being silently ignored.

    Parameters
    ----------
    environment : Mapping[str, str]
        Injected environment holding the configured routes.

    Returns
    -------
    tuple[str, ...]
        `RUSTC_WRAPPER` and then `RUSTC_WORKSPACE_WRAPPER` when configured.

    Raises
    ------
    ProverToolError
        A selected route is malformed, or Cargo configuration supplies a
        compiler route the injected environment cannot prove.
    """
    check_process_tool_selections(environment)
    return tuple(
        environment[variable]
        for variable in ("RUSTC_WRAPPER", "RUSTC_WORKSPACE_WRAPPER")
        if variable in environment
    )


def runtime_search_path(runtime: Path, inherited: str) -> str:
    """Put the matching runtime first and drop other Rustup toolchain libs.

    Parameters
    ----------
    runtime : Path
        Library directory of the compiler the child must load.
    inherited : str
        Inherited `LD_LIBRARY_PATH`, possibly empty.

    Returns
    -------
    str
        `runtime` followed by every inherited entry that does not name a
        `toolchains/<name>/lib` directory, so a host toolchain cannot shadow
        the selected one.
    """

    def compatible(entry: str) -> bool:
        """Return whether the path lacks a toolchains/<name>/lib suffix."""
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
