"""Thin CLI registration for inspectable, supervised Kani harness execution.

`kani harnesses list` prints the resolved inventory as deterministic JSON.
`kani harnesses run` executes one exact invocation per selected harness, writes
live human evidence and the final table to standard output, optionally writes
versioned JSONL, and writes the final JSON report when asked. Errors go to
standard error.

Signal handling lives here and nowhere else. The library installs no handlers,
so an embedding application keeps control; this adapter installs SIGINT and
SIGTERM for the duration of one run, finalizes the report on interruption, and
returns ``128 + signal``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import signal
import sys
import threading
import typing as typ
from pathlib import Path  # ruff: ignore[typing-only-standard-library-import] - Cyclopts evaluates annotations

import cyclopts
from cyclopts import Parameter

from rust_prover_tools.cli_support import repo_root as resolve_repo_root
from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.exit_status import print_lines
from rust_prover_tools.kani_harness_run import (
    KaniHarnessDependencies,
    KaniHarnessRunOptions,
    LegacyRoute,
    SnapshotRoute,
    run_harnesses,
)
from rust_prover_tools.kani_inventory import (
    discover_inventory,
    inventory_document,
    load_inventory,
    restrict_inventory,
)
from rust_prover_tools.kani_snapshot.pins import load_pin

if typ.TYPE_CHECKING:
    import collections.abc as cabc

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
)

__all__ = ("SignalInterrupt", "harnesses_app")

#: Default available-memory floor in MiB, from the source plan.
DEFAULT_WATCHDOG_MIB: typ.Final[int] = 1024

#: Errors this adapter reports as an ordinary nonzero exit.
_EXPECTED_ERRORS = (ProverToolError, ValueError)

harnesses_app: cyclopts.App = cyclopts.App(
    "harnesses",
    help="Inspect and run Kani harnesses under supervision",
    config=(),
)


class SignalInterrupt:
    """A SIGINT/SIGTERM source the supervision layer can await.

    The library never installs a handler. This adapter owns the installation,
    restoration, and translation of a signal into an awaitable stop, so an
    embedding application's own signal policy is untouched.
    """

    def __init__(self) -> None:
        """Create a source that has received no signal."""
        self._signum: int | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._event: asyncio.Event | None = None
        self._previous: dict[int, object] = {}

    @property
    def signum(self) -> int | None:
        """The interrupting signal, or `None` while the run may continue."""
        return self._signum

    def bind(self, loop: asyncio.AbstractEventLoop) -> None:
        """Bind the source to the run's event loop."""
        self._loop = loop
        self._event = asyncio.Event()
        if self._signum is not None:
            self._event.set()

    @contextlib.contextmanager
    def installed(self) -> cabc.Generator[None]:
        """Install SIGINT and SIGTERM handlers for the duration of the block."""
        if threading.current_thread() is not threading.main_thread():
            yield
            return
        for signum in (signal.SIGINT, signal.SIGTERM):
            self._previous[signum] = signal.getsignal(signum)
            signal.signal(signum, self._handle)
        try:
            yield
        finally:
            for signum, previous in self._previous.items():
                signal.signal(signum, typ.cast("typ.Any", previous))
            self._previous.clear()

    def _handle(self, signum: int, frame: object) -> None:
        """Record the first signal and release the monitor."""
        del frame
        if self._signum is None:
            self._signum = signum
        if self._loop is not None and self._event is not None:
            self._loop.call_soon_threadsafe(self._event.set)

    async def wait(self) -> None:
        """Wait until a signal asks the run to stop."""
        if self._signum is not None:
            return
        if self._event is None:
            self._event = asyncio.Event()
        await self._event.wait()


@harnesses_app.command(name="list")
def list_harnesses(
    *,
    repo_root: Path | None = None,
    package: typ.Annotated[
        list[str] | None, Parameter(name="--package", allow_repeating=True)
    ] = None,
    inventory: typ.Annotated[Path | None, Parameter(name="--inventory")] = None,
    output: typ.Annotated[Path | None, Parameter(name="--output")] = None,
) -> int:
    """Print the resolved, package-qualified harness inventory as JSON.

    Parameters
    ----------
    repo_root : Path | None
        Repository or workspace root. Defaults to the current directory.
    package : list[str] | None
        Packages to discover, repeatable. Empty selects every package.
    inventory : Path | None
        Authoritative version-1 inventory document, overriding discovery.
    output : Path | None
        Write the JSON here instead of standard output.

    Returns
    -------
    int
        Zero on success, one for an expected discovery or format error.
    """
    try:
        root = resolve_repo_root(repo_root)
        packages = tuple(package or ())
        resolved = (
            restrict_inventory(load_inventory(inventory), packages)
            if inventory is not None
            else discover_inventory(root, packages)
        )
        text = json.dumps(inventory_document(resolved), indent=2)
        if output is None:
            print_lines((text,))
        else:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(text + "\n", encoding="utf-8")
    except _EXPECTED_ERRORS as exc:
        print_lines((str(exc),), stream=sys.stderr)
        return 1
    return 0


@harnesses_app.command(name="run")
# CLI parameter binding needs one parameter per flag and env-var mapping.
# pylint: disable-next=too-many-arguments,too-many-locals
def run_command_harnesses(
    *,
    repo_root: Path | None = None,
    package: typ.Annotated[
        list[str] | None, Parameter(name="--package", allow_repeating=True)
    ] = None,
    harness: typ.Annotated[
        list[str] | None, Parameter(name="--harness", allow_repeating=True)
    ] = None,
    inventory: typ.Annotated[Path | None, Parameter(name="--inventory")] = None,
    installation: typ.Annotated[Path | None, Parameter(name="--installation")] = None,
    cwd: typ.Annotated[Path | None, Parameter(name="--cwd")] = None,
    snapshot_pin: typ.Annotated[Path | None, Parameter(name="--snapshot-pin")] = None,
    cache_dir: typ.Annotated[Path | None, Parameter(name="--cache-dir")] = None,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    sample_interval_seconds: float = DEFAULT_SAMPLE_INTERVAL_SECONDS,
    heartbeat_seconds: float = DEFAULT_HEARTBEAT_SECONDS,
    watchdog: typ.Annotated[bool, Parameter(negative="--no-watchdog")] = True,
    watchdog_mib: int = DEFAULT_WATCHDOG_MIB,
    human: typ.Annotated[Path | None, Parameter(name="--human")] = None,
    jsonl: typ.Annotated[Path | None, Parameter(name="--jsonl")] = None,
    report: typ.Annotated[Path | None, Parameter(name="--report")] = None,
    extra_arg: typ.Annotated[
        list[str] | None, Parameter(name="--extra-arg", allow_repeating=True)
    ] = None,
) -> int:
    """Run every selected harness under supervision.

    Parameters
    ----------
    repo_root : Path | None
        Repository or workspace root. Defaults to the current directory.
    package : list[str] | None
        Packages to discover, repeatable.
    harness : list[str] | None
        Harness pretty names to run, repeatable.
    inventory : Path | None
        Authoritative version-1 inventory document.
    installation : Path | None
        Qualified Kani release root, for the legacy route.
    cwd : Path | None
        Proof working directory; required with `installation`.
    snapshot_pin : Path | None
        Reviewed snapshot pin, for the snapshot route.
    cache_dir : Path | None
        Snapshot cache root.
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
    human : Path | None
        Append the human job log here instead of standard output.
    jsonl : Path | None
        Append versioned JSONL evidence here.
    report : Path | None
        Write the final JSON report here.
    extra_arg : list[str] | None
        Additional verifier arguments, preserved verbatim.

    Returns
    -------
    int
        Zero only for a complete passing run, one otherwise, and
        ``128 + signal`` when a signal interrupted the run.
    """
    try:
        root = resolve_repo_root(repo_root)
        route = _route(
            installation=installation,
            cwd=cwd,
            snapshot_pin=snapshot_pin,
            cache_dir=cache_dir,
            root=root,
            arguments=tuple(extra_arg or ()),
        )
        config = _config(
            timeout_seconds=timeout_seconds,
            sample_interval_seconds=sample_interval_seconds,
            heartbeat_seconds=heartbeat_seconds,
            watchdog=watchdog,
            watchdog_mib=watchdog_mib,
            report=report,
        )
        interrupt = SignalInterrupt()
        with contextlib.ExitStack() as stack:
            dependencies = KaniHarnessDependencies(
                diagnostics=_diagnostics(stack, config, human, jsonl),
                interrupts=interrupt,
            )
            with interrupt.installed():
                run = run_harnesses(
                    KaniHarnessRunOptions(
                        repo_root=root,
                        route=route,
                        packages=tuple(package or ()),
                        harnesses=tuple(harness or ()),
                        inventory_path=inventory,
                        config=config,
                        dependencies=dependencies,
                    )
                )
    except _EXPECTED_ERRORS as exc:
        print_lines((str(exc),), stream=sys.stderr)
        return 1
    print_lines(run.table)
    return run.exit_status


# pylint: disable-next=too-many-arguments
def _route(
    *,
    installation: Path | None,
    cwd: Path | None,
    snapshot_pin: Path | None,
    cache_dir: Path | None,
    root: Path,
    arguments: tuple[str, ...],
) -> LegacyRoute | SnapshotRoute:
    """Resolve exactly one execution route from the CLI options."""
    if (installation is None) == (snapshot_pin is None):
        msg = "select exactly one of --installation or --snapshot-pin"
        raise ProverToolError(msg)
    if installation is not None:
        if cwd is None:
            msg = "--cwd is required with --installation"
            raise ProverToolError(msg)
        if cache_dir is not None:
            msg = "--cache-dir is only valid with --snapshot-pin"
            raise ProverToolError(msg)
        return LegacyRoute(
            installation=installation,
            cwd=cwd,
            arguments=arguments,
        )
    pin_path = typ.cast("Path", snapshot_pin)
    return SnapshotRoute(
        pin=load_pin(pin_path),
        cache_home=cache_dir,
        cwd=cwd or root,
        arguments=arguments,
    )


# pylint: disable-next=too-many-arguments
def _config(
    *,
    timeout_seconds: float,
    sample_interval_seconds: float,
    heartbeat_seconds: float,
    watchdog: bool,
    watchdog_mib: int,
    report: Path | None,
) -> SupervisionConfig:
    """Translate the CLI flags into the validated policy."""
    threshold = watchdog_mib * 1024 * 1024 if watchdog else None
    return SupervisionConfig(
        timeout_seconds=timeout_seconds,
        sample_interval_seconds=sample_interval_seconds,
        heartbeat_interval_seconds=heartbeat_seconds,
        watchdog_threshold_bytes=threshold,
        sinks=DiagnosticSinks(report_path=report),
    )


def _diagnostics(
    stack: contextlib.ExitStack,
    config: SupervisionConfig,
    human: Path | None,
    jsonl: Path | None,
) -> Diagnostics:
    """Open the live diagnostic destinations for one run."""
    human_sink = HumanSink(sys.stdout, max_line_bytes=config.max_line_bytes)
    if human is not None:
        human.parent.mkdir(parents=True, exist_ok=True)
        human_sink = HumanSink(
            stack.enter_context(human.open("a", encoding="utf-8")),
            max_line_bytes=config.max_line_bytes,
        )
    jsonl_sink: JsonlSink | NullSink = NullSink()
    if jsonl is not None:
        jsonl.parent.mkdir(parents=True, exist_ok=True)
        jsonl_sink = JsonlSink(
            stack.enter_context(jsonl.open("a", encoding="utf-8")),
            path=jsonl,
            max_line_bytes=config.max_line_bytes,
        )
    return Diagnostics(human=human_sink, jsonl=jsonl_sink)
