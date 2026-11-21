"""Structured option binding for `kani harnesses run`.

Cyclopts flattens each dataclass into its fields, so every flag keeps its own
name and help text while the command function takes four coherent groups
instead of seventeen keyword parameters. The same groups translate into the
route, policy, and diagnostics the composition root consumes.
"""

from __future__ import annotations

import dataclasses as dc
import sys
import typing as typ
from pathlib import Path  # ruff: ignore[typing-only-standard-library-import] - Cyclopts evaluates annotations

from cyclopts import Parameter

from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_harness_run import LegacyRoute, SnapshotRoute
from rust_prover_tools.kani_snapshot.pins import load_pin
from rust_prover_tools.supervision import (
    DEFAULT_HEARTBEAT_SECONDS,
    DEFAULT_SAMPLE_INTERVAL_SECONDS,
    DEFAULT_TIMEOUT_SECONDS,
    Diagnostics,
    DiagnosticSinks,
    HumanSink,
    JsonlSink,
    NullSink,
    SupervisionConfig,
    require_distinct_destinations,
)

if typ.TYPE_CHECKING:
    import contextlib

__all__ = (
    "DEFAULT_WATCHDOG_MIB",
    "DestinationFlags",
    "PolicyFlags",
    "RouteFlags",
    "SelectionFlags",
    "open_run_diagnostics",
    "resolve_route",
    "supervision_policy",
)

#: Default available-memory floor in MiB, from the source plan.
DEFAULT_WATCHDOG_MIB: typ.Final[int] = 1024


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
    """

    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    sample_interval_seconds: float = DEFAULT_SAMPLE_INTERVAL_SECONDS
    heartbeat_seconds: float = DEFAULT_HEARTBEAT_SECONDS
    watchdog: typ.Annotated[bool, Parameter(negative="--no-watchdog")] = True
    watchdog_mib: int = DEFAULT_WATCHDOG_MIB


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


def resolve_route(flags: RouteFlags, *, root: Path) -> LegacyRoute | SnapshotRoute:
    """Resolve exactly one execution route from the CLI options.

    Parameters
    ----------
    flags : RouteFlags
        Route selection and verbatim verifier arguments.
    root : Path
        Repository root used as the snapshot route's default working
        directory.

    Returns
    -------
    LegacyRoute | SnapshotRoute
        The single selected route.

    Raises
    ------
    ProverToolError
        Neither or both routes were selected, or a route-specific option is
        missing or misplaced.
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
    pin_path = typ.cast("Path", flags.snapshot_pin)
    return SnapshotRoute(
        pin=load_pin(pin_path),
        cache_home=flags.cache_dir,
        cwd=flags.cwd or root,
        arguments=arguments,
    )


def supervision_policy(
    policy: PolicyFlags,
    destinations: DestinationFlags,
) -> SupervisionConfig:
    """Translate the CLI flags into the validated policy.

    Parameters
    ----------
    policy : PolicyFlags
        Per-harness budget, sampling, heartbeat, and watchdog settings.
    destinations : DestinationFlags
        Evidence and report destinations.

    Returns
    -------
    SupervisionConfig
        The validated policy.

    Raises
    ------
    ValueError
        A timing is unusable or two destinations name one file.
    """
    require_distinct_destinations(
        human_path=destinations.human,
        jsonl_path=destinations.jsonl,
        report_path=destinations.report,
    )
    threshold = policy.watchdog_mib * 1024 * 1024 if policy.watchdog else None
    return SupervisionConfig(
        timeout_seconds=policy.timeout_seconds,
        sample_interval_seconds=policy.sample_interval_seconds,
        heartbeat_interval_seconds=policy.heartbeat_seconds,
        watchdog_threshold_bytes=threshold,
        sinks=DiagnosticSinks(report_path=destinations.report),
    )


def open_run_diagnostics(
    stack: contextlib.ExitStack,
    config: SupervisionConfig,
    destinations: DestinationFlags,
) -> Diagnostics:
    """Open the live diagnostic destinations for one run.

    Parameters
    ----------
    stack : contextlib.ExitStack
        Stack that owns every handle opened here.
    config : SupervisionConfig
        Validated policy supplying the line bound.
    destinations : DestinationFlags
        Human and JSONL destinations, or `None` for standard output and off.

    Returns
    -------
    Diagnostics
        The sinks for this run.
    """
    human = destinations.human
    human_sink = HumanSink(sys.stdout, max_line_bytes=config.max_line_bytes)
    if human is not None:
        human.parent.mkdir(parents=True, exist_ok=True)
        human_sink = HumanSink(
            stack.enter_context(human.open("a", encoding="utf-8")),
            max_line_bytes=config.max_line_bytes,
        )
    jsonl = destinations.jsonl
    jsonl_sink: JsonlSink | NullSink = NullSink()
    if jsonl is not None:
        jsonl.parent.mkdir(parents=True, exist_ok=True)
        existing = jsonl.stat().st_size if jsonl.exists() else 0
        jsonl_sink = JsonlSink(
            stack.enter_context(jsonl.open("a", encoding="utf-8")),
            path=jsonl,
            max_line_bytes=config.max_line_bytes,
            start_offset=existing,
        )
    return Diagnostics(human=human_sink, jsonl=jsonl_sink)
