"""Qualify one Kani release installation and run it with typed stage events.

Qualification is a single sequence: validate the installation and Cargo
routing, build the child environment, then probe the rustc, compiler, and
verifier identities. A supervised run performs that sequence once and derives
every harness invocation from the result, so no harness can observe a
different installation or environment from the one whose identities were
checked.

Stage events stay bounded: they carry a stage name, release, duration, status
class, and error category, never a path, argument list, or captured output.
"""

from __future__ import annotations

import dataclasses as dc
import typing as typ
from functools import partial

from rust_prover_tools import kani_release
from rust_prover_tools.commands import CommandSpec
from rust_prover_tools.errors import CommandFailedError, ProverToolError
from rust_prover_tools.kani_exec_routing import admission_chain
from rust_prover_tools.kani_release import (
    RUSTC_IDENTITY,
    KaniExecOptions,
    observe_stage,
    prepare_kani_command,
    prepare_kani_installation,
)
from rust_prover_tools.kani_release_config import checked_resolve

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

__all__ = (
    "QualifiedKaniCommand",
    "exec_kani",
    "qualify_kani_command",
    "qualify_kani_installation",
)


@dc.dataclass(frozen=True, slots=True)
class QualifiedKaniCommand:
    """A Kani installation whose environment and identities were checked once.

    Attributes
    ----------
    spec : CommandSpec
        Fully composed specification including the literal arguments supplied
        at qualification time.
    base_argv : tuple[str, ...]
        Driver and subcommand, without any verifier arguments.
    arguments : tuple[str, ...]
        Literal arguments supplied at qualification time.
    installation : Path
        Validated release root.
    chain : tuple[str, ...]
        Admission wrapper chain the identity probes used.
    """

    spec: CommandSpec
    base_argv: tuple[str, ...]
    arguments: tuple[str, ...]
    installation: Path
    chain: tuple[str, ...]

    def command(self, arguments: cabc.Sequence[str]) -> CommandSpec:
        """Return one invocation derived from this qualified base.

        Parameters
        ----------
        arguments : Sequence[str]
            Complete verifier argument vector for one harness.

        Returns
        -------
        CommandSpec
            The derived specification, preserving cwd, environment, and every
            other base property without rebuilding them.
        """
        return dc.replace(self.spec, argv=(*self.base_argv, *arguments))


def qualify_kani_command(
    options: KaniExecOptions, environment: cabc.Mapping[str, str]
) -> QualifiedKaniCommand:
    """Validate one release installation and probe its identities once.

    Parameters
    ----------
    options : KaniExecOptions
        Explicit release installation, proof cwd, and literal arguments.
    environment : Mapping[str, str]
        Injected inherited environment, including admission wrappers.

    Returns
    -------
    QualifiedKaniCommand
        A base whose environment and identities have been checked once.

    Raises
    ------
    ProverToolError
        The release identity or verification route could not be qualified.
    CommandFailedError
        An identity query failed before proof execution.
    """
    return _qualify(options, environment, validate_arguments=True)


def qualify_kani_installation(
    options: KaniExecOptions, environment: cabc.Mapping[str, str]
) -> QualifiedKaniCommand:
    """Validate a release installation for supervised per-harness execution.

    Unlike `qualify_kani_command`, the literal argument vector may be empty,
    because the supervisor supplies a composed selector for each harness and
    validates that composed vector before it runs. Installation, Cargo routing,
    environment, and identity contracts are identical.

    Parameters
    ----------
    options : KaniExecOptions
        Explicit release installation and proof cwd.
    environment : Mapping[str, str]
        Injected inherited environment, including admission wrappers.

    Returns
    -------
    QualifiedKaniCommand
        A base whose environment and identities have been checked once.

    Raises
    ------
    ProverToolError
        The release identity or verification route could not be qualified.
    """
    return _qualify(options, environment, validate_arguments=False)


def _qualify(
    options: KaniExecOptions,
    environment: cabc.Mapping[str, str],
    *,
    validate_arguments: bool,
) -> QualifiedKaniCommand:
    """Validate, probe, and return one qualified Kani base."""
    if validate_arguments:
        spec = observe_stage(
            "validation",
            partial(prepare_kani_command, options, environment),
        )
        root = checked_resolve(options.installation)
    else:
        spec, root = observe_stage(
            "validation",
            partial(prepare_kani_installation, options, environment),
        )
    chain = admission_chain(environment)
    for stage, relative in (
        ("rustc_identity", "toolchain/bin/rustc"),
        ("compiler_identity", "bin/kani-compiler"),
    ):
        observe_stage(
            stage,
            partial(
                _checked_identity,
                spec,
                (*chain, str(root / relative), "--version"),
                RUSTC_IDENTITY,
            ),
        )
    observe_stage("verifier_identity", partial(_checked_verifier, spec))
    return QualifiedKaniCommand(
        spec=spec,
        base_argv=(spec.argv[0], "kani"),
        arguments=options.arguments,
        installation=root,
        chain=chain,
    )


def _checked_identity(spec: CommandSpec, argv: tuple[str, ...], expected: str) -> None:
    """Query a selected tool without masking nonzero status or identity drift."""
    result = kani_release.run_command(CommandSpec(argv, cwd=spec.cwd, env=spec.env))
    if result.exit_code != 0:
        raise CommandFailedError(argv[0], result.exit_code, result.stderr)
    if result.stdout.strip() != expected:
        msg = (
            "Kani selected binary identity does not match the qualified "
            "release metadata"
        )
        raise ProverToolError(msg)


def _checked_verifier(spec: CommandSpec) -> None:
    """Accept only the qualified stable driver's complete version response."""
    version = kani_release.run_command(
        CommandSpec((spec.argv[0], "kani", "--version"), cwd=spec.cwd, env=spec.env)
    )
    if version.exit_code != 0:
        raise CommandFailedError(spec.argv[0], version.exit_code, version.stderr)
    if (version.stdout + version.stderr).strip() not in {
        "kani 0.67.0",
        "cargo-kani 0.67.0",
    }:
        msg = "Kani selected verifier identity is outside the qualified 0.67.0 release"
        raise ProverToolError(msg)


def exec_kani(options: KaniExecOptions, environment: cabc.Mapping[str, str]) -> int:
    """Validate identities and stream a legacy release invocation through Cuprum.

    Parameters
    ----------
    options : KaniExecOptions
        Explicit release and proof invocation.
    environment : Mapping[str, str]
        Injected inherited environment, including admission wrappers.

    Returns
    -------
    int
        Child status, including negative signal status, without success masking.

    Raises
    ------
    ProverToolError
        The release identity or verification route could not be qualified.
    CommandFailedError
        An identity query failed before proof execution.
    """
    qualified = qualify_kani_command(options, environment)
    return observe_stage(
        "proof",
        partial(kani_release.run_command, dc.replace(qualified.spec, echo=True)),
        status_of=lambda result: result.exit_code,
    ).exit_code
