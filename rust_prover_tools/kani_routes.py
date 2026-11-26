"""Qualify one Kani route once and derive every harness invocation from it.

A run must not observe one installation, generation, environment, or grammar
while launching another. These types therefore hold the result of one
qualification and expose a builder that derives each harness's command from
that single base. The legacy and snapshot routes keep their own contracts:
argument validity, Cargo routing, identity probes, and the active-generation
check are unchanged from their single-invocation forms.
"""

from __future__ import annotations

import dataclasses as dc
import os
import typing as typ
from pathlib import Path

from cuprum.context import EnvMode

from rust_prover_tools.kani_cli_grammar import KaniGrammar, grammar_for
from rust_prover_tools.kani_inventory import InventoryHarness, compose_arguments
from rust_prover_tools.kani_qualification import qualify_kani_installation
from rust_prover_tools.kani_release import (
    KANI_RELEASE_VERSION,
    KaniExecOptions,
    validate_kani_arguments,
)
from rust_prover_tools.kani_snapshot.identity import validate_forwarded_args
from rust_prover_tools.kani_snapshot.run import (
    ProofContext,
    SnapshotOutput,
    prepare_snapshot_proof,
)
from rust_prover_tools.kani_snapshot.run import (
    RunOptions as SnapshotRunOptions,
)
from rust_prover_tools.supervision import LaunchCommand, LaunchEnvironment

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from rust_prover_tools.commands import CommandSpec
    from rust_prover_tools.kani_snapshot.install_model import InstallHooks
    from rust_prover_tools.kani_snapshot.models import SnapshotPin

__all__ = (
    "KaniRoute",
    "LegacyRoute",
    "PreparedRoute",
    "SnapshotRoute",
    "prepare_route",
    "route_working_directory",
)


@dc.dataclass(frozen=True, slots=True)
class LegacyRoute:
    """An already installed, qualified Kani release.

    Attributes
    ----------
    installation : Path
        Release root containing ``bin`` and ``toolchain``.
    cwd : Path
        Proof working directory.
    arguments : tuple[str, ...]
        Additional verifier arguments, preserved verbatim.
    environment : cabc.Mapping[str, str] | None
        Inherited environment, or `None` for the process environment.
    """

    installation: Path
    cwd: Path
    arguments: tuple[str, ...] = ()
    environment: cabc.Mapping[str, str] | None = None


@dc.dataclass(frozen=True, slots=True)
class SnapshotRoute:
    """One immutable Kani snapshot generation.

    Attributes
    ----------
    pin : SnapshotPin
        Reviewed snapshot pin.
    cache_home : Path | None
        Snapshot cache root.
    cwd : Path | None
        Proof working directory, or `None` for the caller's.
    arguments : tuple[str, ...]
        Additional verifier arguments, preserved verbatim.
    environment : cabc.Mapping[str, str] | None
        Inherited environment, or `None` for the process environment.
    hooks : InstallHooks | None
        Probe operation used while revalidating the generation.
    """

    pin: SnapshotPin
    cache_home: Path | None = None
    cwd: Path | None = None
    arguments: tuple[str, ...] = ()
    environment: cabc.Mapping[str, str] | None = None
    hooks: InstallHooks | None = None


type KaniRoute = LegacyRoute | SnapshotRoute
"""The two supported Kani execution routes."""


@dc.dataclass(frozen=True, slots=True)
class PreparedRoute:
    """The route facts shared by every harness invocation."""

    grammar: KaniGrammar
    kind: str
    build: cabc.Callable[[InventoryHarness], LaunchCommand]


def route_working_directory(route: KaniRoute, *, process_cwd: Path) -> Path:
    """Return the working directory the route will actually use.

    Parameters
    ----------
    route : KaniRoute
        Selected legacy or snapshot route.
    process_cwd : Path
        Process working directory the caller resolved, used only when a
        snapshot route names none.

    Returns
    -------
    Path
        The proof working directory. This function never reads the process
        working directory itself.
    """
    if isinstance(route, LegacyRoute):
        return route.cwd
    return process_cwd if route.cwd is None else route.cwd


def _launch_environment(command: CommandSpec) -> LaunchEnvironment:
    """Render one command's environment without Cuprum's types."""
    values: dict[str, str | None] = {}
    for name, value in (command.env or {}).items():
        values[name] = value if isinstance(value, str) else None
    return LaunchEnvironment(
        values=values,
        replace=command.env_mode is EnvMode.REPLACE,
    )


def prepare_route(
    route: KaniRoute,
    *,
    environ: cabc.Mapping[str, str] | None = None,
    cwd: Path | None = None,
) -> PreparedRoute:
    """Qualify the selected route once and return its per-harness builder.

    Parameters
    ----------
    route : KaniRoute
        Route to qualify.
    environ : cabc.Mapping[str, str] | None
        Inherited environment. `None` reads the process environment at this
        call, which is the explicit provider a caller can replace in a test.
    cwd : Path | None
        Process working directory a route without one inherits. `None` reads
        the process working directory at this call.
    """
    provider = os.environ if environ is None else environ
    process_cwd = Path.cwd() if cwd is None else cwd
    if isinstance(route, LegacyRoute):
        return _prepare_legacy(route, provider)
    return _prepare_snapshot(route, provider, process_cwd)


def _prepare_legacy(
    route: LegacyRoute,
    environ: cabc.Mapping[str, str],
) -> PreparedRoute:
    """Qualify the legacy release, its identities, and its environment.

    The route carries its own environment when one was supplied; otherwise this
    function uses the caller's provider, so qualification consumes exactly one
    mapping and never reads the process environment itself.
    """
    inherited = route.environment if route.environment is not None else environ
    environment = dict(inherited)
    qualified = qualify_kani_installation(
        KaniExecOptions(route.installation, route.cwd, ()),
        environment,
    )
    grammar = grammar_for(KANI_RELEASE_VERSION)

    def build(harness: InventoryHarness) -> LaunchCommand:
        """Compose one legacy invocation for `harness`."""
        arguments = compose_arguments(harness, grammar, route.arguments)
        validate_kani_arguments(arguments)
        command = qualified.command(arguments)
        return LaunchCommand(
            argv=command.argv,
            cwd=command.cwd,
            env=_launch_environment(command),
        )

    return PreparedRoute(grammar=grammar, kind="legacy", build=build)


def _prepare_snapshot(
    route: SnapshotRoute,
    environ: cabc.Mapping[str, str],
    process_cwd: Path,
) -> PreparedRoute:
    """Qualify the snapshot generation, environment, and grammar once.

    The route's own environment wins when it has one, exactly as the legacy
    route's does; otherwise the caller's provider is used. Preparation never
    inspects the process itself, so a route can be qualified against exactly
    one mapping.
    """
    inherited = route.environment if route.environment is not None else environ
    qualified = prepare_snapshot_proof(
        SnapshotRunOptions(
            pin=route.pin,
            args=(),
            cache_home=route.cache_home,
        ),
        route.hooks,
        context=ProofContext(
            cwd=route.cwd if route.cwd is not None else process_cwd,
            environment=inherited,
        ),
        validate_arguments=False,
    )
    grammar = grammar_for(qualified.backend_version)

    def build(harness: InventoryHarness) -> LaunchCommand:
        """Compose one snapshot invocation for `harness`."""
        arguments = compose_arguments(harness, grammar, route.arguments)
        validate_forwarded_args(arguments)
        command = qualified.command(
            arguments,
            SnapshotOutput(echo=False, capture=False),
        )
        return LaunchCommand(
            argv=command.argv,
            cwd=command.cwd,
            env=_launch_environment(command),
        )

    return PreparedRoute(grammar=grammar, kind="snapshot", build=build)
