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

from rust_prover_tools.kani_inventory import InventoryHarness, compose_arguments
from rust_prover_tools.kani_log import KaniGrammar, grammar_for
from rust_prover_tools.kani_qualification import qualify_kani_installation
from rust_prover_tools.kani_release import (
    KANI_RELEASE_VERSION,
    KaniExecOptions,
    validate_kani_arguments,
)
from rust_prover_tools.kani_snapshot.identity import validate_forwarded_args
from rust_prover_tools.kani_snapshot.run import (
    RunOptions as SnapshotRunOptions,
)
from rust_prover_tools.kani_snapshot.run import (
    SnapshotOutput,
    prepare_snapshot_proof,
)
from rust_prover_tools.supervision import LaunchCommand

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

    from rust_prover_tools.kani_snapshot.install_model import InstallHooks
    from rust_prover_tools.kani_snapshot.models import SnapshotPin

__all__ = (
    "KaniRoute",
    "LegacyRoute",
    "PreparedRoute",
    "SnapshotRoute",
    "prepare_route",
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


def prepare_route(route: KaniRoute) -> PreparedRoute:
    """Qualify the selected route once and return its per-harness builder."""
    if isinstance(route, LegacyRoute):
        return _prepare_legacy(route)
    return _prepare_snapshot(route)


def _prepare_legacy(route: LegacyRoute) -> PreparedRoute:
    """Qualify the legacy release, its identities, and its environment."""
    environment = dict(os.environ if route.environment is None else route.environment)
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
            env=command.env,
            env_mode=command.env_mode,
        )

    return PreparedRoute(grammar=grammar, kind="legacy", build=build)


def _prepare_snapshot(route: SnapshotRoute) -> PreparedRoute:
    """Qualify the snapshot generation, environment, and grammar once."""
    qualified = prepare_snapshot_proof(
        SnapshotRunOptions(
            pin=route.pin,
            args=(),
            cache_home=route.cache_home,
            cwd=route.cwd,
        ),
        route.hooks,
        environment=route.environment,
        validate_arguments=False,
    )
    grammar = grammar_for(qualified.backend_version)

    def build(harness: InventoryHarness) -> LaunchCommand:
        """Compose one legacy invocation for `harness`."""
        arguments = compose_arguments(harness, grammar, route.arguments)
        validate_forwarded_args(arguments)
        command = qualified.command(
            arguments,
            SnapshotOutput(echo=False, capture=False),
        )
        return LaunchCommand(
            argv=command.argv,
            cwd=command.cwd,
            env=command.env,
        )

    return PreparedRoute(grammar=grammar, kind="snapshot", build=build)
