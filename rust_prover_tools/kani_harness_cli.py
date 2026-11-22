"""Thin CLI registration for inspectable, supervised Kani harness execution.

`kani harnesses list` prints the resolved inventory as deterministic JSON.
`kani harnesses run` executes one exact invocation per selected harness, writes
live human evidence and the final table to standard output, optionally writes
versioned JSONL, and writes the final JSON report when asked. Errors go to
standard error.

Signal handling lives here and nowhere else. The library installs no handlers,
so an embedding application keeps control; this adapter installs SIGINT and
SIGTERM for the duration of the supervised execution phase, finalizes the
report on interruption, and returns ``128 + signal``. Discovery and route
qualification run before those handlers exist, so a signal there ends the
process under its normal disposition instead of being recorded for a phase
that has not started.
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
from rust_prover_tools.kani_harness_cli_options import (
    DestinationFlags,
    PolicyFlags,
    RouteFlags,
    SelectionFlags,
    open_run_diagnostics,
    resolve_route,
    supervision_policy,
)
from rust_prover_tools.kani_harness_run import (
    KaniHarnessDependencies,
    KaniHarnessRunOptions,
    execute_harnesses,
    prepare_harnesses,
)
from rust_prover_tools.kani_inventory import (
    discover_inventory,
    inventory_document,
    load_inventory,
    restrict_inventory,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc


__all__ = ("SignalInterrupt", "harnesses_app")

#: Errors this adapter reports as an ordinary nonzero exit. A destination the
#: process cannot open is a usage or environment error, not a crash.
_EXPECTED_ERRORS = (ProverToolError, ValueError, OSError)

_DEFAULT_SELECTION = SelectionFlags()
_DEFAULT_ROUTE = RouteFlags()
_DEFAULT_POLICY = PolicyFlags()
_DEFAULT_DESTINATIONS = DestinationFlags()

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
        self._notify()

    def _notify(self) -> None:
        """Release the monitor when its loop is still usable."""
        loop = self._loop
        event = self._event
        if loop is None or event is None:
            return
        if not loop.is_closed():
            loop.call_soon_threadsafe(event.set)

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
def run_command_harnesses(
    *,
    selection: typ.Annotated[SelectionFlags, Parameter(name="*")] = (
        _DEFAULT_SELECTION
    ),
    routing: typ.Annotated[RouteFlags, Parameter(name="*")] = _DEFAULT_ROUTE,
    policy: typ.Annotated[PolicyFlags, Parameter(name="*")] = _DEFAULT_POLICY,
    destinations: typ.Annotated[DestinationFlags, Parameter(name="*")] = (
        _DEFAULT_DESTINATIONS
    ),
) -> int:
    """Run every selected harness under supervision.

    Parameters
    ----------
    selection : SelectionFlags
        Repository, packages, and harness names.
    routing : RouteFlags
        Qualified route and verbatim verifier arguments.
    policy : PolicyFlags
        Per-harness budget, sampling, and watchdog policy.
    destinations : DestinationFlags
        Live evidence and final report destinations.

    Returns
    -------
    int
        Zero only for a complete passing run, one otherwise, and
        ``128 + signal`` when a signal interrupted the run.
    """
    executing = False
    try:
        root = resolve_repo_root(selection.repo_root)
        route = resolve_route(routing, root=root)
        config = supervision_policy(policy, destinations)
        preparation = prepare_harnesses(
            KaniHarnessRunOptions(
                repo_root=root,
                route=route,
                packages=tuple(selection.package or ()),
                harnesses=tuple(selection.harness or ()),
                inventory_path=selection.inventory,
                config=config,
            )
        )
        interrupt = SignalInterrupt()
        with interrupt.installed(), contextlib.ExitStack() as stack:
            dependencies = KaniHarnessDependencies(
                diagnostics=open_run_diagnostics(stack, config, destinations),
                interrupts=interrupt,
            )
            executing = True
            run = execute_harnesses(preparation, config, dependencies)
    except KeyboardInterrupt:
        # Preparation runs under the application's normal signal disposition,
        # so SIGINT arrives here as KeyboardInterrupt and ends it immediately.
        phase = "execution" if executing else "preparation"
        print_lines((f"run {phase} interrupted",), stream=sys.stderr)
        return 128 + signal.SIGINT
    except _EXPECTED_ERRORS as exc:
        print_lines((str(exc),), stream=sys.stderr)
        return 1
    print_lines(run.table)
    if interrupt.signum is not None:
        return 128 + interrupt.signum
    return run.exit_status
