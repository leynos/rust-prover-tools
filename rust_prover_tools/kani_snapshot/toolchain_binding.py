"""Verification of extracted toolchain bytes before any compiler runs.

Setup installs a Rust toolchain and then a probe executes `rustc` and
`kani-compiler`. A version string is an assertion by those binaries about
themselves, not evidence about their bytes, so the reviewed manifest also
records the digest of the installed compiler, of the extracted libraries the
binary loads, and of the rustup component list. This module compares the
installed tree with those records before the probe runs.

Rustup component artifacts are recorded with their URLs, sizes and digests for
audit and reproducibility. They are not re-derived here: installation never
re-downloads a component, so there are no downloaded bytes to compare. The
extracted files that a probe actually executes or loads are what this module
verifies.
"""

from __future__ import annotations

import dataclasses as dc
import os
import stat
import typing as typ
from pathlib import Path

from rust_prover_tools.downloads import sha256_file
from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_snapshot.progress import Progress, as_checkpoint
from rust_prover_tools.kani_snapshot.toolchain_name import is_safe_toolchain_name
from rust_prover_tools.kani_snapshot.validation import is_contained_relative_path

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from rust_prover_tools.kani_snapshot.models import SnapshotManifest
    from rust_prover_tools.kani_snapshot.snapshot_layout import SnapshotRuntime

__all__ = ("toolchain_root", "verify_toolchain")

#: Installed compiler the probe executes.
_RUSTC: typ.Final[str] = "bin/rustc"

#: Rustup's record of the components installed in one toolchain.
_RUSTUP_COMPONENTS: typ.Final[str] = "lib/rustlib/components"

#: Directory whose shared objects the pinned dynamic-loader path exposes.
_LIBRARY_DIRECTORY: typ.Final[str] = "lib"


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

    Raises
    ------
    ProverToolError
        Raised when the identifier is not one safe toolchain name or the
        resolved toolchain escapes the private rustup home.
    """
    name = runtime.toolchain
    if not is_safe_toolchain_name(name):
        msg = f"snapshot toolchain identifier must be one plain name: {name!r}"
        raise ProverToolError(msg)
    toolchains = runtime.layout.rustup_home / "toolchains"
    root = toolchains / name
    home = _resolve(runtime.layout.rustup_home, "private rustup home")
    resolved = _resolve(root, "private toolchain")
    if not resolved.is_relative_to(home):
        msg = f"snapshot toolchain escapes the private rustup home: {root}"
        raise ProverToolError(msg)
    return root


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
    checks = (
        ("compiler", _RUSTC, toolchain.rustc_sha256),
        ("component records", _RUSTUP_COMPONENTS, toolchain.rustup_components_sha256),
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


def _require_matching(
    tree: _ToolchainTree,
    relative: str,
    expected: str,
    description: str,
) -> None:
    """Require one contained toolchain file to match its recorded digest."""
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
    actual = sha256_file(path, checkpoint=tree.progress)
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
    Walking the directory rejects that gap, and any library or directory link
    that resolves outside the toolchain.
    """
    root = tree.root / _LIBRARY_DIRECTORY
    for path in _walk_library_directory(tree, root, set()):
        relative = path.relative_to(tree.root).as_posix()
        if relative not in recorded:
            msg = (
                "snapshot toolchain provides a shared library the manifest "
                f"does not record: {relative}"
            )
            raise ProverToolError(msg)


def _walk_library_directory(
    tree: _ToolchainTree,
    directory: Path,
    visited: set[Path],
) -> cabc.Iterator[Path]:
    """Yield shared objects below one directory, following contained links."""
    tree.progress()
    if not directory.is_dir():
        return
    resolved = _resolve(directory, "toolchain library directory")
    if not resolved.is_relative_to(tree.resolved):
        msg = f"snapshot toolchain library directory escapes: {directory}"
        raise ProverToolError(msg)
    if resolved in visited:
        # A link cycle is harmless once its real directory has been walked.
        return
    visited.add(resolved)
    for child in _list_directory(directory):
        tree.progress()
        path = Path(child.path)
        if child.is_dir(follow_symlinks=True):
            yield from _walk_library_directory(tree, path, visited)
        elif child.is_file(follow_symlinks=True) and _is_shared_library(child.name):
            _require_contained(path, tree, "library")
            yield path


def _list_directory(directory: Path) -> list[os.DirEntry[str]]:
    """Return one directory's entries, reporting inspection failures."""
    try:
        with os.scandir(directory) as children:
            return list(children)
    except OSError as exc:
        msg = f"failed to inspect snapshot toolchain library directory: {exc}"
        raise ProverToolError(msg) from exc


def _is_shared_library(name: str) -> bool:
    """Return whether a file name denotes an ELF shared object."""
    return name.endswith(".so") or ".so." in name
