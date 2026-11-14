"""Execution of a proof through the active snapshot generation.

Execution never installs, never falls back to a host Kani and never changes the
user's environment. It revalidates the generation, rejects setup or backend
selectors, isolates the child's Cargo routing so a repository profile cannot
replace the pinned compiler, and then runs the pinned launcher with the
verifier's arguments unchanged, mirroring the launcher's stdout and stderr as
they are produced, retaining only a bounded tail of each stream and preserving
its exit status.
"""

from __future__ import annotations

import dataclasses as dc
import logging
import os
import typing as typ
from pathlib import Path

from rust_prover_tools.commands import (
    CommandResult,
    CommandSpec,
    TailCapture,
    run_command,
)
from rust_prover_tools.kani_snapshot.identity import (
    SnapshotRuntime,
    snapshot_layout,
    validate_forwarded_args,
)
from rust_prover_tools.kani_snapshot.install import (
    InstallOptions,
    check_active_snapshot,
)
from rust_prover_tools.kani_snapshot.verification_env import proof_environment
from rust_prover_tools.observability import operation_span

if typ.TYPE_CHECKING:
    from rust_prover_tools.kani_snapshot.install_model import InstallHooks
    from rust_prover_tools.kani_snapshot.models import SnapshotPin

__all__ = ("RunOptions", "run_snapshot")

_LOGGER = logging.getLogger(__name__)


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
    cwd : Path | None
        Proof working directory, normally the repository root. `None`
        inherits the caller's working directory.
    """

    pin: SnapshotPin
    args: tuple[str, ...] = ()
    cache_home: Path | None = None
    cwd: Path | None = None


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
        The launcher's exact exit status with a bounded tail of its stdout and
        stderr. The same output is mirrored to the caller's streams as the
        launcher writes it, so a long proof stays visible while it runs
        without letting its volume size the CLI's memory.

    Raises
    ------
    ProverToolError
        Raised when the snapshot is not installed, has drifted from its
        manifest, the forwarded arguments would replace the backend, or the
        repository's Cargo routing cannot be proved safe.
    """
    with operation_span(_LOGGER, "kani.snapshot.run") as span:
        validate_forwarded_args(options.args)
        # The identity names the exact generation that was just validated.
        # Resolving the activation records again would let a concurrent repair
        # switch the active generation between the check and the launch, so
        # execution uses the checked identity rather than a second,
        # unvalidated resolution.
        identity = check_active_snapshot(
            InstallOptions(pin=options.pin, cache_home=options.cache_home),
            hooks,
        )
        layout = snapshot_layout(
            options.pin,
            cache_home=options.cache_home,
            generation=identity.generation.name,
        )
        runtime = SnapshotRuntime(
            layout=layout,
            toolchain=identity.toolchain,
            backend_version=identity.backend_version,
        )
        working_directory = Path.cwd() if options.cwd is None else options.cwd
        env = proof_environment(runtime, os.environ, cwd=working_directory.resolve())
        # `echo=True` mirrors each stream live, so the CLI must not replay the
        # result. Capture stays off because a proof log can be arbitrarily
        # large; the line hook keeps only the tail that explains an outcome.
        tail = TailCapture()
        # The launcher is the process boundary a proof spends its time in, so
        # it gets its own span inside the overall run.
        with operation_span(_LOGGER, "kani.snapshot.launch"):
            result = run_command(
                CommandSpec(
                    (str(runtime.launcher), *options.args),
                    cwd=options.cwd,
                    env=env,
                    echo=True,
                    capture=False,
                    on_line=tail,
                )
            )
        span.finish(
            "success" if result.exit_code == 0 else "failure",
            exit_code=result.exit_code,
        )
        return CommandResult(
            stdout=tail.text("stdout"),
            stderr=tail.text("stderr"),
            exit_code=result.exit_code,
        )
