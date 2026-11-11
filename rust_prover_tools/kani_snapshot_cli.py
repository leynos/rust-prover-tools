"""Thin CLI registration for isolated Kani snapshots.

Install and check default to `tools/kani/snapshot.toml` below the repository
root and keep cache state under `.kani-snapshots`. Run requires an explicit pin
and forwards every argument after `--` unchanged.
"""

from __future__ import annotations

import dataclasses as dc
import typing as typ
from pathlib import Path  # ruff: ignore[typing-only-standard-library-import] - Cyclopts evaluates annotations

import cyclopts
from cyclopts import Parameter

from rust_prover_tools.cli_support import error_exit
from rust_prover_tools.cli_support import repo_root as resolve_repo_root
from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.exit_status import print_lines
from rust_prover_tools.kani_snapshot.install import check_snapshot, install_snapshot
from rust_prover_tools.kani_snapshot.install_model import InstallOptions
from rust_prover_tools.kani_snapshot.pins import load_pin
from rust_prover_tools.kani_snapshot.run import RunOptions, run_snapshot
from rust_prover_tools.path_resolution import resolve_configured_path

if typ.TYPE_CHECKING:
    from rust_prover_tools.kani_snapshot.identity import SnapshotIdentity

__all__ = ("kani_snapshot_run", "run_app", "snapshot_app")

#: Default reviewed pin below the repository root.
DEFAULT_PIN: typ.Final[str] = "tools/kani/snapshot.toml"

#: Default snapshot cache below the repository root.
DEFAULT_CACHE_DIR: typ.Final[str] = ".kani-snapshots"

snapshot_app: cyclopts.App = cyclopts.App(
    "snapshot",
    help="Install and check one immutable Kani snapshot",
    config=(),
)


@dc.dataclass(frozen=True, slots=True)
class _Paths:
    """Resolved repository, pin and cache locations for one command."""

    root: Path
    pin: Path
    cache: Path


def _paths(
    *,
    pin: Path | None,
    repo_root: Path | None,
    cache_dir: Path | None,
) -> _Paths:
    """Resolve the pin and cache paths, defaulting below the repository root."""
    root = resolve_repo_root(repo_root)
    return _Paths(
        root=root,
        pin=resolve_configured_path(pin, root, root / DEFAULT_PIN),
        cache=resolve_configured_path(cache_dir, root, root / DEFAULT_CACHE_DIR),
    )


def _identity_lines(verb: str, identity: SnapshotIdentity) -> tuple[str, ...]:
    """Render the stable success summary for install and check."""
    return (
        f"snapshot {identity.tag} {verb} at {identity.generation}",
        f"rustc: {identity.rustc_vv}",
        f"kani: {identity.backend_version}",
    )


@snapshot_app.command(name="install")
def install(
    *,
    pin: Path | None = None,
    repo_root: Path | None = None,
    cache_dir: Path | None = None,
    asset_dir: Path | None = None,
) -> int:
    """Install a reviewed snapshot from a local verified asset directory.

    Parameters
    ----------
    pin : Path | None
        Reviewed pin. Defaults to `tools/kani/snapshot.toml`.
    repo_root : Path | None
        Repository root used for default paths.
    cache_dir : Path | None
        Snapshot cache. Defaults to `.kani-snapshots`.
    asset_dir : Path | None
        Directory holding the release files and their attestation bundle.

    Returns
    -------
    int
        Exit code, where zero indicates a validated active generation.
    """
    try:
        paths = _paths(pin=pin, repo_root=repo_root, cache_dir=cache_dir)
        snapshot_pin = load_pin(paths.pin)
        identity = install_snapshot(
            InstallOptions(
                pin=snapshot_pin,
                assets_dir=asset_dir,
                cache_home=paths.cache,
            )
        )
    except ProverToolError as exc:
        return error_exit(exc)
    print_lines(_identity_lines("installed", identity))
    return 0


@snapshot_app.command(name="check")
def check(
    *,
    pin: Path | None = None,
    repo_root: Path | None = None,
    cache_dir: Path | None = None,
) -> int:
    """Check that the active generation still matches its reviewed pin.

    Parameters
    ----------
    pin : Path | None
        Reviewed pin. Defaults to `tools/kani/snapshot.toml`.
    repo_root : Path | None
        Repository root used for default paths.
    cache_dir : Path | None
        Snapshot cache. Defaults to `.kani-snapshots`.

    Returns
    -------
    int
        Exit code, where zero indicates a matching generation.
    """
    try:
        paths = _paths(pin=pin, repo_root=repo_root, cache_dir=cache_dir)
        snapshot_pin = load_pin(paths.pin)
        identity = check_snapshot(
            InstallOptions(pin=snapshot_pin, cache_home=paths.cache)
        )
    except ProverToolError as exc:
        return error_exit(exc)
    print_lines(_identity_lines("checked", identity))
    return 0


run_app: cyclopts.App = cyclopts.App(
    "run",
    help=(
        "Run a proof through the exact installed snapshot. "
        "Place -- before verifier arguments."
    ),
    config=(),
)


def kani_snapshot_run(
    *args: typ.Annotated[str, Parameter(allow_leading_hyphen=True)],
    snapshot_pin: Path,
    repo_root: Path | None = None,
    cache_dir: Path | None = None,
) -> int:
    """Run the pinned launcher with every argument after `--` unchanged.

    Parameters
    ----------
    *args : str
        Verifier arguments forwarded verbatim.
    snapshot_pin : Path
        Required reviewed pin for this proof.
    repo_root : Path | None
        Repository root; also the proof working directory.
    cache_dir : Path | None
        Snapshot cache. Defaults to `.kani-snapshots`.

    Returns
    -------
    int
        The launcher's exact exit status. Its output is already mirrored while
        the proof runs, so the captured copy is not replayed here.
    """
    try:
        paths = _paths(pin=snapshot_pin, repo_root=repo_root, cache_dir=cache_dir)
        result = run_snapshot(
            RunOptions(
                pin=load_pin(paths.pin),
                args=tuple(args),
                cache_home=paths.cache,
                cwd=paths.root,
            )
        )
    except ProverToolError as exc:
        return error_exit(exc)
    return result.exit_code


run_app.default_command = kani_snapshot_run
