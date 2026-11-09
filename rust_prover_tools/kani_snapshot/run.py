"""Execution of a proof through the active snapshot generation.

Execution never installs, never falls back to a host Kani and never changes the
user's environment. It revalidates the generation, rejects setup or backend
selectors, and then replaces itself with the pinned launcher while preserving
the verifier's arguments, streams and exit status.
"""

from __future__ import annotations

import dataclasses as dc
import os
import typing as typ

from rust_prover_tools.commands import CommandResult, CommandSpec, run_command
from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_snapshot.identity import (
    execution_environment,
    runtime_for,
    snapshot_layout,
    validate_forwarded_args,
)
from rust_prover_tools.kani_snapshot.install import InstallOptions, check_snapshot
from rust_prover_tools.kani_snapshot.install_state import (
    active_manifest,
    load_active,
)

if typ.TYPE_CHECKING:
    from pathlib import Path

    from rust_prover_tools.kani_snapshot.install_model import InstallHooks
    from rust_prover_tools.kani_snapshot.models import SnapshotPin

__all__ = ("RunOptions", "run_snapshot")


@dc.dataclass(frozen=True, slots=True)
class RunOptions:
    """One proof invocation against an installed snapshot.

    Attributes
    ----------
    pin : SnapshotPin
        Exact snapshot the proof must run on.
    args : tuple[str, ...]
        Arguments forwarded unchanged after `--`.
    cache_home : Path | None
        Explicit cache root holding the installed generation.
    """

    pin: SnapshotPin
    args: tuple[str, ...] = ()
    cache_home: Path | None = None


def run_snapshot(
    options: RunOptions,
    hooks: InstallHooks | None = None,
) -> CommandResult:
    """Run the pinned launcher with the forwarded verifier arguments.

    Parameters
    ----------
    options : RunOptions
        Snapshot pin, forwarded arguments and cache root.
    hooks : InstallHooks | None
        Probe operation used while revalidating the generation.

    Returns
    -------
    CommandResult
        Captured stdout, stderr and the launcher's exact exit status.

    Raises
    ------
    ProverToolError
        Raised when the snapshot is not installed, has drifted from its
        manifest, or the forwarded arguments would replace the backend.
    """
    validate_forwarded_args(options.args)
    check_snapshot(
        InstallOptions(pin=options.pin, cache_home=options.cache_home),
        hooks,
    )
    layout = snapshot_layout(options.pin, cache_home=options.cache_home)
    pointer = load_active(layout)
    if pointer is None:
        msg = "snapshot is not installed; run install first"
        raise ProverToolError(msg)
    generation = snapshot_layout(
        options.pin,
        cache_home=options.cache_home,
        generation=pointer.generation,
    )
    manifest = active_manifest(options.pin, layout)
    runtime = runtime_for(generation, manifest)
    env = execution_environment(runtime, os.environ)
    return run_command(CommandSpec((str(runtime.launcher), *options.args), env=env))
