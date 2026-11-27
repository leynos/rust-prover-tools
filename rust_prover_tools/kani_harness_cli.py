"""Thin CLI registration for inspectable, supervised Kani harness execution.

`kani harnesses list` prints the resolved inventory as deterministic JSON.
`kani harnesses run` executes one exact invocation per selected harness, writes
live human evidence and the final table to standard output, optionally writes
versioned JSONL, and writes the final JSON report when asked. Errors go to
standard error.

This module only registers the commands and runs their flow. Signal handling
lives in `kani_harness_signal`, which installs SIGINT and SIGTERM for the
duration of the supervised execution phase so an embedding application keeps
control. Discovery and route qualification run before those handlers exist, so
a signal there ends the process under its normal disposition instead of being
recorded for a phase that has not started.
"""

from __future__ import annotations

import json
import signal
import sys
import typing as typ
from pathlib import Path  # ruff: ignore[typing-only-standard-library-import] - Cyclopts evaluates annotations

import cyclopts
from cyclopts import Parameter

from rust_prover_tools.cli_support import repo_root as resolve_repo_root
from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.exit_status import print_lines
from rust_prover_tools.kani_destinations import (
    require_stdout_free,
    supervision_policy,
)
from rust_prover_tools.kani_diagnostics import prepare_and_open_diagnostics
from rust_prover_tools.kani_harness_cli_options import (
    DestinationFlags,
    PolicyFlags,
    RouteFlags,
    SelectionFlags,
    resolve_route,
)
from rust_prover_tools.kani_harness_run import (
    KaniHarnessDependencies,
    KaniHarnessRun,
    KaniHarnessRunOptions,
    execute_harnesses,
    prepare_harnesses,
)
from rust_prover_tools.kani_harness_signal import (
    SignalInterrupt,
    install_interrupt_handlers,
)
from rust_prover_tools.kani_inventory import (
    discover_inventory,
    inventory_document,
    load_inventory,
    restrict_inventory,
    write_inventory,
)
from rust_prover_tools.kani_snapshot.pins import load_pin
from rust_prover_tools.supervision import render_table

if typ.TYPE_CHECKING:
    from rust_prover_tools.kani_harness_run import KaniHarnessPreparation
    from rust_prover_tools.supervision import SupervisionConfig


__all__ = ("harnesses_app",)

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
        if output is None:
            print_lines((json.dumps(inventory_document(resolved), indent=2),))
        else:
            write_inventory(output, resolved)
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
    interrupt = SignalInterrupt()
    executing = False
    try:
        options = _resolve_run(selection, routing, policy, destinations)
        preparation = _prepare_run(options)
        executing = True
        run = _execute_run(preparation, options.config, destinations, interrupt)
    except KeyboardInterrupt:
        # Preparation runs under the application's normal signal disposition,
        # so SIGINT arrives here as KeyboardInterrupt and ends it immediately.
        phase = "execution" if executing else "preparation"
        print_lines((f"run {phase} interrupted",), stream=sys.stderr)
        return 128 + signal.SIGINT
    except _EXPECTED_ERRORS as exc:
        print_lines((str(exc),), stream=sys.stderr)
        if interrupt.signum is not None:
            return 128 + interrupt.signum
        return 1
    return _present_run(run, interrupt)


def _resolve_run(
    selection: SelectionFlags,
    routing: RouteFlags,
    policy: PolicyFlags,
    destinations: DestinationFlags,
) -> KaniHarnessRunOptions:
    """Resolve the requested root and route into one validated run request."""
    root = resolve_repo_root(selection.repo_root)
    selected_snapshot = (
        routing.snapshot_pin is not None and routing.installation is None
    )
    pin = load_pin(routing.snapshot_pin) if selected_snapshot else None
    return KaniHarnessRunOptions(
        repo_root=root,
        route=resolve_route(routing, root=root, pin=pin),
        packages=tuple(selection.package or ()),
        harnesses=tuple(selection.harness or ()),
        inventory_path=selection.inventory,
        config=supervision_policy(policy, destinations),
    )


def _prepare_run(options: KaniHarnessRunOptions) -> KaniHarnessPreparation:
    """Discover, select, and qualify the request without running it."""
    return prepare_harnesses(options)


def _execute_run(
    preparation: KaniHarnessPreparation,
    config: SupervisionConfig,
    destinations: DestinationFlags,
    interrupt: SignalInterrupt,
) -> KaniHarnessRun:
    """Supervise the preparation while owning handlers and open handles."""
    require_stdout_free(destinations.jsonl, "--jsonl")
    require_stdout_free(destinations.report, "--report")
    with (
        install_interrupt_handlers(interrupt),
        prepare_and_open_diagnostics(config, None, human_stream=sys.stdout) as opened,
    ):
        dependencies = KaniHarnessDependencies(
            diagnostics=opened,
            interrupts=interrupt,
        )
        return execute_harnesses(preparation, config, dependencies)


def _present_run(run: KaniHarnessRun, interrupt: SignalInterrupt) -> int:
    """Print one completed run and return its process exit status."""
    print_lines(render_table(run.report))
    if interrupt.signum is not None:
        return 128 + interrupt.signum
    return run.exit_status
