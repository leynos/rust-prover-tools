"""Structured option binding for `kani harnesses run`.

Cyclopts flattens each dataclass into its fields, so every flag keeps its own
name and help text while the command function takes four coherent groups
instead of seventeen keyword parameters. The same groups translate into the
route, policy, and diagnostics the composition root consumes.
"""

from __future__ import annotations

import dataclasses as dc
import typing as typ
from pathlib import Path  # ruff: ignore[typing-only-standard-library-import] - Cyclopts evaluates annotations

from cyclopts import Parameter

from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_harness_run import LegacyRoute, SnapshotRoute
from rust_prover_tools.supervision import (
    DEFAULT_HEARTBEAT_SECONDS,
    DEFAULT_SAMPLE_INTERVAL_SECONDS,
    DEFAULT_TIMEOUT_SECONDS,
)

if typ.TYPE_CHECKING:
    from rust_prover_tools.kani_snapshot.models import SnapshotPin

__all__ = (
    "DEFAULT_MAX_LOG_MIB",
    "DEFAULT_WATCHDOG_MIB",
    "DestinationFlags",
    "PolicyFlags",
    "RouteFlags",
    "SelectionFlags",
    "resolve_route",
)

#: Default available-memory floor in MiB, from the source plan.
DEFAULT_WATCHDOG_MIB: typ.Final[int] = 1024
#: Default retained-log budget for the final parse, in MiB.
DEFAULT_MAX_LOG_MIB: typ.Final[int] = 64


@dc.dataclass(frozen=True, slots=True)
class SelectionFlags:
    """What to discover and which harnesses to run.

    Attributes
    ----------
    repo_root : Path | None
        Repository or workspace root. Defaults to the current directory.
    package : list[str] | None
        Packages to discover, repeatable.
    harness : list[str] | None
        Harness pretty names to run, repeatable.
    inventory : Path | None
        Authoritative version-1 inventory document.
    """

    repo_root: Path | None = None
    package: typ.Annotated[
        list[str] | None, Parameter(name="--package", allow_repeating=True)
    ] = None
    harness: typ.Annotated[
        list[str] | None, Parameter(name="--harness", allow_repeating=True)
    ] = None
    inventory: typ.Annotated[Path | None, Parameter(name="--inventory")] = None


@dc.dataclass(frozen=True, slots=True)
class RouteFlags:
    """Which qualified route to run and what to pass it.

    Attributes
    ----------
    installation : Path | None
        Qualified Kani release root, for the legacy route.
    cwd : Path | None
        Proof working directory; required with `installation`.
    snapshot_pin : Path | None
        Reviewed snapshot pin, for the snapshot route.
    cache_dir : Path | None
        Snapshot cache root.
    extra_arg : list[str] | None
        Additional verifier arguments, preserved verbatim.
    """

    installation: typ.Annotated[Path | None, Parameter(name="--installation")] = None
    cwd: typ.Annotated[Path | None, Parameter(name="--cwd")] = None
    snapshot_pin: typ.Annotated[Path | None, Parameter(name="--snapshot-pin")] = None
    cache_dir: typ.Annotated[Path | None, Parameter(name="--cache-dir")] = None
    extra_arg: typ.Annotated[
        list[str] | None, Parameter(name="--extra-arg", allow_repeating=True)
    ] = None


@dc.dataclass(frozen=True, slots=True)
class PolicyFlags:
    """How each harness is supervised.

    Attributes
    ----------
    timeout_seconds : float
        Wall-clock budget per harness.
    sample_interval_seconds : float
        Resource sampling interval.
    heartbeat_seconds : float
        Heartbeat interval while the prover is silent.
    watchdog : bool
        Stop a harness when host available memory falls below the floor.
    watchdog_mib : int
        Available-memory floor in MiB.
    max_log_mib : int
        Largest verifier log retained for the final parse, in MiB.
    """

    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    sample_interval_seconds: float = DEFAULT_SAMPLE_INTERVAL_SECONDS
    heartbeat_seconds: float = DEFAULT_HEARTBEAT_SECONDS
    watchdog: typ.Annotated[bool, Parameter(negative="--no-watchdog")] = True
    watchdog_mib: int = DEFAULT_WATCHDOG_MIB
    max_log_mib: int = DEFAULT_MAX_LOG_MIB


@dc.dataclass(frozen=True, slots=True)
class DestinationFlags:
    """Where live evidence and the final report are written.

    Attributes
    ----------
    human : Path | None
        Append the human job log here instead of standard output.
    jsonl : Path | None
        Append versioned JSONL evidence here.
    report : Path | None
        Write the final JSON report here.
    """

    human: typ.Annotated[Path | None, Parameter(name="--human")] = None
    jsonl: typ.Annotated[Path | None, Parameter(name="--jsonl")] = None
    report: typ.Annotated[Path | None, Parameter(name="--report")] = None


def resolve_route(
    flags: RouteFlags,
    *,
    root: Path,
    pin: SnapshotPin | None = None,
) -> LegacyRoute | SnapshotRoute:
    """Resolve exactly one execution route from the CLI options.

    Parameters
    ----------
    flags : RouteFlags
        Route selection and verbatim verifier arguments.
    root : Path
        Repository root used as the snapshot route's default working
        directory.
    pin : SnapshotPin | None
        Pin already loaded by the caller. Required with `--snapshot-pin`; the
        loader is kept out of route resolution so this function stays a pure
        mapping from validated flags to a route.

    Returns
    -------
    LegacyRoute | SnapshotRoute
        The single selected route.

    Raises
    ------
    ProverToolError
        Neither or both routes were selected, a route-specific option is
        missing or misplaced, or a snapshot pin was not loaded first.
    """
    if (flags.installation is None) == (flags.snapshot_pin is None):
        msg = "select exactly one of --installation or --snapshot-pin"
        raise ProverToolError(msg)
    arguments = tuple(flags.extra_arg or ())
    if flags.installation is not None:
        if flags.cwd is None:
            msg = "--cwd is required with --installation"
            raise ProverToolError(msg)
        if flags.cache_dir is not None:
            msg = "--cache-dir is only valid with --snapshot-pin"
            raise ProverToolError(msg)
        return LegacyRoute(
            installation=flags.installation,
            cwd=flags.cwd,
            arguments=arguments,
        )
    if pin is None:
        msg = "the snapshot pin must be loaded before the route is resolved"
        raise ProverToolError(msg)
    return SnapshotRoute(
        pin=pin,
        cache_home=flags.cache_dir,
        cwd=flags.cwd or root,
        arguments=arguments,
    )
