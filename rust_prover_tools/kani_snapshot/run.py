"""Execution of a proof through the active snapshot generation.

Execution never installs, never falls back to a host Kani, and never changes
the user's environment. The route has two halves. `prepare_snapshot_proof`
revalidates the generation once, rejects setup or backend selectors, isolates
the child's Cargo routing, and builds the proof environment;
`run_snapshot` runs exactly one forwarded invocation through that prepared
base. Supervised per-harness execution derives every invocation from the same
prepared base, so one run reads one activation and one environment regardless
of how many harnesses it executes.

Standard output and standard error are mirrored as they are produced, with
only a bounded tail retained, and the launcher's exit status is preserved.
"""

from __future__ import annotations

import dataclasses as dc
import logging
import os
import typing as typ
from pathlib import Path

from cuprum.context import EnvMode

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
from rust_prover_tools.kani_snapshot.verification_env import (
    frozen_proof_environment,
)
from rust_prover_tools.observability import operation_span

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum import LineHook
    from cuprum.context import UnsetType

    from rust_prover_tools.kani_snapshot.install_model import InstallHooks
    from rust_prover_tools.kani_snapshot.models import SnapshotPin

__all__ = (
    "ProofContext",
    "QualifiedSnapshotProof",
    "RunOptions",
    "SnapshotOutput",
    "prepare_snapshot_proof",
    "run_snapshot",
)

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


@dc.dataclass(frozen=True, slots=True)
class ProofContext:
    """The caller-resolved working directory and environment for one proof.

    Preparation never reads the process environment or working directory
    itself: the composition root supplies both, so a route can be qualified
    against exactly one mapping and one directory.

    Attributes
    ----------
    cwd : Path
        Proof working directory, already resolved by the caller.
    environment : collections.abc.Mapping[str, str]
        Inherited environment the caller selected.
    """

    cwd: Path
    environment: cabc.Mapping[str, str]


@dc.dataclass(frozen=True, slots=True)
class SnapshotOutput:
    """Output handling for one snapshot invocation.

    Attributes
    ----------
    echo : bool
        Mirror the child's output to the caller's streams.
    capture : bool
        Retain the child's output on the result.
    on_line : LineHook | None
        Line hook for bounded observation.
    """

    echo: bool
    capture: bool
    on_line: LineHook | None = None


@dc.dataclass(frozen=True, slots=True)
class QualifiedSnapshotProof:
    """A snapshot generation validated once, ready for one or more harnesses.

    Attributes
    ----------
    runtime : SnapshotRuntime
        Validated generation, toolchain, and launcher.
    cwd : Path
        Absolute proof working directory that preparation checked.
    environment : cabc.Mapping[str, str | UnsetType]
        Child environment overlay built once for this generation.
    arguments : tuple[str, ...]
        Literal arguments supplied at qualification time.
    backend_version : str
        Kani version the snapshot generation reports.
    """

    runtime: SnapshotRuntime
    cwd: Path
    environment: cabc.Mapping[str, str | UnsetType]
    arguments: tuple[str, ...]
    backend_version: str

    @property
    def base_argv(self) -> tuple[str, ...]:
        """The launcher path, without verifier arguments."""
        return (str(self.runtime.launcher),)

    def command(
        self,
        arguments: cabc.Sequence[str],
        output: SnapshotOutput,
    ) -> CommandSpec:
        """Return one invocation derived from this qualified base.

        Parameters
        ----------
        arguments : Sequence[str]
            Complete verifier argument vector for one invocation.
        output : SnapshotOutput
            Echo, capture, and line-hook choices.

        Returns
        -------
        CommandSpec
            The derived command, preserving the prepared environment.
        """
        return CommandSpec(
            (*self.base_argv, *arguments),
            cwd=self.cwd,
            env=self.environment,
            env_mode=EnvMode.REPLACE,
            echo=output.echo,
            capture=output.capture,
            on_line=output.on_line,
        )


def prepare_snapshot_proof(
    options: RunOptions,
    hooks: InstallHooks | None = None,
    *,
    context: ProofContext,
    validate_arguments: bool = True,
) -> QualifiedSnapshotProof:
    """Validate the active generation and build the proof environment once.

    Parameters
    ----------
    options : RunOptions
        Snapshot pin, forwarded arguments and cache root.
    hooks : InstallHooks | None
        Probe operation used while revalidating the generation.
    context : ProofContext
        Caller-resolved working directory and inherited environment.
    validate_arguments : bool
        Whether to validate the literal forwarded arguments. Supervised
        execution passes `False` and validates each composed vector instead.

    Returns
    -------
    QualifiedSnapshotProof
        The validated generation and its prepared child environment.

    Raises
    ------
    ProverToolError
        Raised when the snapshot is not installed, has drifted from its
        manifest, the forwarded arguments would replace the backend, or the
        repository's Cargo routing cannot be proved safe.
    """
    if validate_arguments:
        validate_forwarded_args(options.args)
    # The identity names the exact generation that was just validated.
    # Resolving the activation records again would let a concurrent repair
    # switch the active generation between the check and the launch, so
    # execution uses the checked identity rather than a second, unvalidated
    # resolution.
    identity = check_active_snapshot(
        InstallOptions(pin=options.pin, cache_home=options.cache_home),
        hooks,
        environment=context.environment,
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
    working_directory = context.cwd.resolve()
    base = context.environment
    return QualifiedSnapshotProof(
        runtime=runtime,
        cwd=working_directory,
        environment=frozen_proof_environment(
            runtime,
            base,
            cwd=working_directory,
        ),
        arguments=options.args,
        backend_version=runtime.backend_version,
    )


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
        qualified = prepare_snapshot_proof(
            options,
            hooks,
            context=ProofContext(
                cwd=Path.cwd() if options.cwd is None else options.cwd,
                environment=os.environ,
            ),
        )
        # `echo=True` mirrors each stream live, so the CLI must not replay the
        # result. Capture stays off because a proof log can be arbitrarily
        # large; the line hook keeps only the tail that explains an outcome.
        tail = TailCapture()
        with operation_span(_LOGGER, "kani.snapshot.launch"):
            result = run_command(
                qualified.command(
                    qualified.arguments,
                    SnapshotOutput(echo=True, capture=False, on_line=tail),
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
