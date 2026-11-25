"""Verification of extracted toolchain bytes before any compiler runs.

Setup installs a Rust toolchain and then a probe executes `rustc` and
`kani-compiler`. A version string is an assertion by those binaries about
themselves, not evidence about their bytes, so the reviewed manifest also
records the digest of the installed compiler, of the extracted libraries the
binary loads, and of the rustup component list. This module compares the
installed tree with those records before the probe runs.

Rustup writes `lib/rustlib/components` in nondeterministic component-completion
order, so the recorded digest is of the canonical sorted names, not the file's
raw bytes. Any change to the set is still detected; a reordering is not.
Rustup component artifacts are recorded with their URLs, sizes and digests for
audit and reproducibility. They are not re-derived here: installation never
re-downloads a component, so there are no downloaded bytes to compare. The
extracted files that a probe actually executes or loads are what this module
verifies.
"""

from __future__ import annotations

import dataclasses as dc
import stat
import typing as typ

from rust_prover_tools.downloads import sha256_file
from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_snapshot.components_file import (
    components_digest_of_file,
)
from rust_prover_tools.kani_snapshot.progress import Progress, as_checkpoint
from rust_prover_tools.kani_snapshot.shared_libraries import shared_libraries
from rust_prover_tools.kani_snapshot.toolchain_name import is_safe_toolchain_name
from rust_prover_tools.kani_snapshot.validation import is_contained_relative_path

if typ.TYPE_CHECKING:
    from pathlib import Path

    from rust_prover_tools.kani_snapshot.models import SnapshotManifest
    from rust_prover_tools.kani_snapshot.snapshot_layout import SnapshotRuntime

__all__ = ("toolchain_root", "verify_toolchain")

#: Installed compiler the probe executes.
_RUSTC: typ.Final[str] = "bin/rustc"

#: Rustup's record of the components installed in one toolchain.
_RUSTUP_COMPONENTS: typ.Final[str] = "lib/rustlib/components"


@dc.dataclass(frozen=True, slots=True)
class _ToolchainTree:
    """One installed toolchain plus the budget its checks report to."""

    root: Path
    resolved: Path
    progress: Progress


def toolchain_root(runtime: SnapshotRuntime) -> Path:
    """Return the generation-private toolchain root for one runtime.

    The identifier reaches this join from the manifest, so it is revalidated
    here as one plain toolchain name and its resolved path must remain inside
    the generation's private rustup home. A manifest therefore cannot select an
    absolute path, a parent traversal or a symbolic link out of the generation.
    The rustup home itself must also resolve inside the generation: checking
    only that the toolchain sits below the home would accept a home that setup
    replaced with a link to anywhere else.

    Raises
    ------
    ProverToolError
        Raised when the identifier is not one safe toolchain name, the private
        rustup home escapes the generation, or the resolved toolchain escapes
        the private rustup home.
    """
    name = _require_safe_name(runtime.toolchain)
    generation = _resolve(runtime.layout.generation_dir, "snapshot generation")
    home = _require_private_home(runtime, generation)
    root = home / "toolchains" / name
    _require_contained_toolchain(root, home)
    return root


def _require_safe_name(name: str) -> str:
    """Require one plain toolchain name and return it."""
    if not is_safe_toolchain_name(name):
        msg = f"snapshot toolchain identifier must be one plain name: {name!r}"
        raise ProverToolError(msg)
    return name


def _require_private_home(runtime: SnapshotRuntime, generation: Path) -> Path:
    """Require the rustup home to resolve inside the generation."""
    home = _resolve(runtime.layout.rustup_home, "private rustup home")
    if not home.is_relative_to(generation):
        msg = (
            f"snapshot rustup home escapes its generation: {runtime.layout.rustup_home}"
        )
        raise ProverToolError(msg)
    return home


def _require_contained_toolchain(root: Path, home: Path) -> None:
    """Require the toolchain to resolve inside the private rustup home."""
    resolved = _resolve(root, "private toolchain")
    if not resolved.is_relative_to(home):
        msg = f"snapshot toolchain escapes the private rustup home: {root}"
        raise ProverToolError(msg)


def verify_toolchain(
    runtime: SnapshotRuntime,
    manifest: SnapshotManifest,
    *,
    checkpoint: Progress | None = None,
) -> None:
    """Require installed toolchain bytes to match the reviewed manifest.

    Parameters
    ----------
    runtime : SnapshotRuntime
        Runtime naming the toolchain that setup installed.
    manifest : SnapshotManifest
        Manifest whose toolchain records name the expected bytes.
    checkpoint : Progress | None
        Called while files and library coverage are verified, so an expired
        installation budget interrupts a large toolchain scan. `None` is a
        no-op.

    Raises
    ------
    ProverToolError
        Raised when the compiler, a recorded library or the rustup component
        list is missing, is not a regular file, has a different digest, or
        when the manifest omits a shared library the toolchain provides.
        Every check runs before the probe, so a mismatch can never be followed
        by executing the bytes it rejected.
    """
    root = toolchain_root(runtime)
    tree = _ToolchainTree(
        root=root,
        resolved=_resolve(root, "private toolchain"),
        progress=as_checkpoint(checkpoint),
    )
    toolchain = manifest.toolchain
    _require_components(tree, toolchain.rustup_components_sha256)
    checks = (
        ("compiler", _RUSTC, toolchain.rustc_sha256),
        *(
            (f"library '{library.path}'", library.path, library.sha256)
            for library in toolchain.libraries
        ),
    )
    for description, relative, expected in checks:
        tree.progress()
        _require_matching(tree, relative, expected, description)
    # Digest-checking only the recorded paths would leave an omitted compiler
    # library unverified even though the pinned dynamic-loader path would load
    # it, so coverage is checked in both directions.
    _require_library_coverage(
        tree,
        {library.path for library in toolchain.libraries},
    )


def _resolve(path: Path, description: str) -> Path:
    """Resolve one path, reporting unreadable paths and symbolic-link loops."""
    try:
        return path.resolve()
    except (OSError, RuntimeError) as exc:
        msg = f"failed to resolve snapshot {description} '{path}': {exc}"
        raise ProverToolError(msg) from exc


def _require_regular(
    tree: _ToolchainTree,
    relative: str,
    description: str,
) -> Path:
    """Require one contained, regular toolchain file and return its path."""
    if not is_contained_relative_path(relative):
        msg = f"snapshot toolchain {description} is not a contained relative path"
        raise ProverToolError(msg)
    path = tree.root / relative
    try:
        status = path.stat()
    except OSError as exc:
        msg = f"snapshot toolchain {description} is missing: {exc}"
        raise ProverToolError(msg) from exc
    if not stat.S_ISREG(status.st_mode):
        msg = f"snapshot toolchain {description} is not a regular file: {path}"
        raise ProverToolError(msg)
    _require_contained(path, tree, description)
    return path


def _require_matching(
    tree: _ToolchainTree,
    relative: str,
    expected: str,
    description: str,
) -> None:
    """Require one contained toolchain file to match its recorded digest."""
    path = _require_regular(tree, relative, description)
    actual = sha256_file(path, checkpoint=tree.progress)
    if actual != expected:
        msg = (
            f"snapshot toolchain {description} does not match the manifest: "
            f"expected {expected}, got {actual}"
        )
        raise ProverToolError(msg)


def _require_components(tree: _ToolchainTree, expected: str) -> None:
    """Require the installed component names to match their recorded set.

    The digest covers the canonical sorted names rather than the file's raw
    bytes, because rustup's write order is not deterministic. The bounded read
    is `components_file.components_digest_of_file`, the same query the producer
    measures with, so both sides share one ceiling and one failure text.
    """
    description = "component records"
    path = _require_regular(tree, _RUSTUP_COMPONENTS, description)
    tree.progress()
    actual = components_digest_of_file(path)
    if actual != expected:
        msg = (
            f"snapshot toolchain {description} does not match the manifest: "
            f"expected {expected}, got {actual}"
        )
        raise ProverToolError(msg)


def _require_contained(path: Path, tree: _ToolchainTree, description: str) -> None:
    """Require a path's resolved target to stay inside the toolchain."""
    if not _resolve(path, description).is_relative_to(tree.resolved):
        msg = f"snapshot toolchain {description} escapes its toolchain: {path}"
        raise ProverToolError(msg)


def _require_library_coverage(tree: _ToolchainTree, recorded: set[str]) -> None:
    """Require every shared object the toolchain exposes to be recorded.

    The proof environment puts `<toolchain>/lib` first on `LD_LIBRARY_PATH`, so
    a shared object there is loadable even when the manifest does not list it.
    The walk itself lives in `shared_libraries`, which the producer uses to
    decide what to record: one definition means the producer cannot publish a
    toolchain this check would refuse.
    """
    for relative in shared_libraries(tree.root, tree.progress):
        if relative not in recorded:
            msg = (
                "snapshot toolchain provides a shared library the manifest "
                f"does not record: {relative}"
            )
            raise ProverToolError(msg)
