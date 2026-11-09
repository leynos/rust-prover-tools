"""Execution environment and identity reporting for a snapshot generation.

Execution never inherits a host Kani, toolchain or compiler selection. It keeps
admission wrappers and `RUSTDOC` so compiler admission still works, but every
path the proof sees is generation-private. The layout itself lives in
`snapshot_layout`.
"""

from __future__ import annotations

import dataclasses as dc
import os
import typing as typ

from cuprum.context import UNSET, UnsetType

from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_snapshot.snapshot_layout import (
    SnapshotLayout,
    SnapshotRuntime,
    identity_directory,
    is_safe_generation_name,
    new_generation_name,
    runtime_for,
    snapshot_layout,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

    from rust_prover_tools.kani_snapshot.models import (
        SnapshotManifest,
        SnapshotPin,
    )

__all__ = (
    "SnapshotIdentity",
    "SnapshotLayout",
    "SnapshotRuntime",
    "build_identity_report",
    "execution_environment",
    "identity_directory",
    "is_safe_generation_name",
    "new_generation_name",
    "runtime_for",
    "snapshot_layout",
    "validate_forwarded_args",
)

#: Inherited variables that would select a different compiler or distribution.
_REMOVED_VARIABLES: typ.Final[tuple[str, ...]] = (
    "RUSTC",
    "RUSTFLAGS",
    "RUSTUP_DIST_SERVER",
    "RUSTUP_UPDATE_ROOT",
    "RUSTUP_DIST_ROOT",
)


#: Setup-only launcher flags. Their presence means setup would run again.
_SETUP_FLAGS: typ.Final[tuple[str, ...]] = (
    "--use-local-bundle",
    "--use-local-toolchain",
)

#: Short flag that enables unstable Kani features, including `lean`.
_UNSTABLE_FLAG: typ.Final[str] = "-Z"


@dc.dataclass(frozen=True, slots=True)
class SnapshotIdentity:
    """The public identity report for one installed generation.

    Attributes
    ----------
    tag : str
        Immutable snapshot tag.
    upstream_commit : str
        Upstream Kani commit.
    builder_commit : str
        Builder commit recorded in the pin.
    manifest_sha256 : str
        Digest of the reviewed manifest bytes.
    target : str
        Rust target triple.
    generation : Path
        Absolute generation directory.
    launcher : Path
        Absolute `kani` launcher path.
    driver : Path
        Absolute `kani-driver` path.
    compiler : Path
        Absolute `kani-compiler` path.
    rustc : Path
        Absolute `rustc` path.
    toolchain : str
        Rust toolchain identifier.
    rustc_vv : str
        Expected `rustc -vV` identity.
    backend_version : str
        Upstream Kani package version.
    """

    tag: str
    upstream_commit: str
    builder_commit: str
    manifest_sha256: str
    target: str
    generation: Path
    launcher: Path
    driver: Path
    compiler: Path
    rustc: Path
    toolchain: str
    rustc_vv: str
    backend_version: str


def build_identity_report(
    pin: SnapshotPin,
    manifest: SnapshotManifest,
    runtime: SnapshotRuntime,
) -> SnapshotIdentity:
    """Return the identity report for one installed generation."""
    return SnapshotIdentity(
        tag=pin.tag,
        upstream_commit=pin.upstream_commit,
        builder_commit=pin.builder_commit,
        manifest_sha256=pin.manifest_sha256,
        target=pin.target,
        generation=runtime.layout.generation_dir,
        launcher=runtime.launcher,
        driver=runtime.driver,
        compiler=runtime.compiler,
        rustc=runtime.rustc,
        toolchain=manifest.toolchain.identifier,
        rustc_vv=manifest.toolchain.rustc_vv,
        backend_version=manifest.backend.version,
    )


def execution_environment(
    runtime: SnapshotRuntime,
    base: cabc.Mapping[str, str | UnsetType],
) -> dict[str, str | UnsetType]:
    """Return a child environment overlay that selects only this generation.

    The result is a Cuprum environment *overlay*, not a replacement mapping.
    Variables the child must not inherit are therefore mapped to
    `cuprum.UNSET`, which removes them from the child without mutating the
    caller's environment. Copying `base` and deleting keys would leave the
    host values active, because an overlay only ever adds or replaces values.

    Compiler and distribution selectors are removed; generation homes, the
    exact toolchain and auto-install suppression are set. Admission wrappers
    and `RUSTDOC` are deliberately preserved.
    """
    env: dict[str, str | UnsetType] = dict.fromkeys(_REMOVED_VARIABLES, UNSET)
    env.update({
        "KANI_HOME": str(runtime.layout.kani_home),
        "RUSTUP_HOME": str(runtime.layout.rustup_home),
        "CARGO_HOME": str(runtime.layout.cargo_home),
        "RUSTUP_TOOLCHAIN": runtime.toolchain,
        "RUSTUP_AUTO_INSTALL": "0",
        "PATH": _prefixed_path(_text_value(base, "PATH"), runtime),
        "PYTHONPATH": _python_path(_text_value(base, "PYTHONPATH"), runtime),
    })
    return env


def _text_value(base: cabc.Mapping[str, str | UnsetType], name: str) -> str:
    """Return an inherited path-list value, treating markers as absent."""
    value = base.get(name)
    return value if isinstance(value, str) else ""


def _prefixed_path(existing: str, runtime: SnapshotRuntime) -> str:
    """Prepend generation launcher directories without repeating our own."""
    own = (str(runtime.kani_dir / "bin"), str(runtime.python_root / "bin"))
    rest = [entry for entry in existing.split(os.pathsep) if entry and entry not in own]
    return os.pathsep.join((*own, *rest))


def _python_path(existing: str, runtime: SnapshotRuntime) -> str:
    """Prepend the bundled `pyroot` without repeating our own entry."""
    own = str(runtime.python_root)
    rest = [entry for entry in existing.split(os.pathsep) if entry and entry != own]
    return os.pathsep.join((own, *rest))


def validate_forwarded_args(args: cabc.Sequence[str]) -> None:
    """Reject arguments that would re-run setup or replace the backend.

    Upstream's launcher recognises `setup` with `--use-local-bundle` or
    `--use-local-toolchain`, and `-Z lean` selects the Lean backend. Everything
    else, including solver and harness selectors, remains transparent.

    Raises
    ------
    ProverToolError
        Raised when a setup command, setup flag or Lean backend selection is
        present.
    """
    if _is_setup_command(args):
        msg = "snapshot execution refuses the upstream setup command"
        raise ProverToolError(msg)
    for index, arg in enumerate(args):
        if arg in _SETUP_FLAGS or arg.startswith(
            tuple(f"{flag}=" for flag in _SETUP_FLAGS)
        ):
            msg = f"snapshot execution refuses upstream setup flag: {arg}"
            raise ProverToolError(msg)
        if _selects_lean(args, index):
            msg = "snapshot execution refuses the Lean backend selection"
            raise ProverToolError(msg)


def _is_setup_command(args: cabc.Sequence[str]) -> bool:
    """Return whether the launcher would interpret the args as setup."""
    if not args:
        return False
    if args[0] == "setup":
        return True
    return len(args) > 1 and args[0] == "kani" and args[1] == "setup"


def _selects_lean(args: cabc.Sequence[str], index: int) -> bool:
    """Return whether the flag at `index` enables the Lean backend."""
    arg = args[index]
    if arg == _UNSTABLE_FLAG:
        following = args[index + 1] if index + 1 < len(args) else ""
        return _has_lean_feature(following)
    if arg.startswith(_UNSTABLE_FLAG) and len(arg) > len(_UNSTABLE_FLAG):
        return _has_lean_feature(arg[len(_UNSTABLE_FLAG) :].lstrip("="))
    return False


def _has_lean_feature(value: str) -> bool:
    """Return whether a `-Z` value list contains `lean`."""
    return any(part.strip() == "lean" for part in value.split(","))
