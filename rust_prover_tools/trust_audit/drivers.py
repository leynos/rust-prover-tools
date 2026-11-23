"""Expand audit arguments into the Cargo target pathnames to scan.

A file argument is its own driver.  A directory argument contributes the Cargo
targets beneath it, discovered by :mod:`.targets`, while a target's physical
file is checked against the repository root.  The driver keeps the pathname
Cargo compiled rather than the resolved link target, because rustc derives a
module's directory from the pathname it loaded.
"""

from __future__ import annotations

import pathlib
import typing as typ

from rust_prover_tools.trust_audit.errors import TrustAuditError
from rust_prover_tools.trust_audit.manifests import CARGO_MANIFEST
from rust_prover_tools.trust_audit.targets import entry_points

if typ.TYPE_CHECKING:
    import collections.abc as cabc

__all__ = ("expand_drivers",)


def expand_drivers(
    root: pathlib.Path,
    paths: cabc.Iterable[pathlib.Path],
) -> tuple[str, ...]:
    """Expand files and crate directories into repository-relative drivers.

    A file is its own driver.  A directory contributes the targets of every
    crate beneath it.  A path that names no directory keeps the ordinary
    single-driver spelling, so a missing file and an outside file still reach
    the audit's own diagnostics.  A directory with no target is refused,
    because an audit that read nothing must not look like a clean one.

    Parameters
    ----------
    root : pathlib.Path
        Resolved repository root that bounds every driver.
    paths : Iterable[pathlib.Path]
        Files or crate directories to start from.

    Returns
    -------
    tuple[str, ...]
        Repository-relative driver pathnames in sorted order, without
        duplicates.

    Raises
    ------
    TrustAuditError
        If a directory, or a target it contains, resolves outside the root, or
        if a directory contributes no target.
    """
    drivers: dict[str, None] = {}
    for path in paths:
        for driver in _drivers_for(root, path):
            drivers[driver] = None
    return tuple(sorted(drivers))


def _drivers_for(
    root: pathlib.Path,
    path: pathlib.Path,
) -> tuple[str, ...]:
    """Return the drivers one argument names, refusing a missing path.

    A directory contributes each Cargo target beneath it.  The target's
    physical file checks containment, but the driver is passed to the audit
    under the pathname Cargo compiles, so a target that is a symbolic link
    resolves its child modules from the linking directory as rustc does.
    """
    candidate = path if path.is_absolute() else root / path
    if not candidate.is_dir():
        return (_relative_to(root, path),)
    resolved = candidate.resolve()
    _require_inside(root, resolved, path)
    drivers = {
        _in_root_spelling(root, entry, path)
        for crate_root in _crate_roots(root, resolved, path)
        for entry in entry_points(root, crate_root)
    }
    if not drivers:
        msg = f"{path} contains no Rust entry point to audit"
        raise TrustAuditError(msg)
    return tuple(sorted(drivers))


def _in_root_spelling(
    root: pathlib.Path,
    entry: pathlib.Path,
    path: pathlib.Path,
) -> str:
    """Return an entry's pathname after checking its resolved file is in root."""
    _require_inside(root, entry.resolve(), path)
    try:
        return entry.relative_to(root).as_posix()
    except ValueError as error:
        msg = (
            f"{entry} names a file inside {root} only through a symbolic link, "
            "so its module directory cannot be derived from the spelling"
        )
        raise TrustAuditError(msg) from error


def _require_inside(
    root: pathlib.Path,
    resolved: pathlib.Path,
    path: pathlib.Path,
) -> None:
    """Refuse a path whose resolved form leaves the repository root."""
    if resolved != root and root not in resolved.parents:
        msg = f"{path} resolves outside the repository root {root}"
        raise TrustAuditError(msg)


def _contained_relative(
    root: pathlib.Path,
    resolved: pathlib.Path,
    path: pathlib.Path,
) -> pathlib.Path:
    """Return a resolved entry's repository-relative path, refusing escape."""
    _require_inside(root, resolved, path)
    return resolved.relative_to(root)


def _crate_roots(
    root: pathlib.Path,
    directory: pathlib.Path,
    path: pathlib.Path,
) -> tuple[pathlib.Path, ...]:
    """Return every crate directory under ``directory``, including itself."""
    manifests = set()
    for manifest in directory.rglob(CARGO_MANIFEST):
        resolved = manifest.parent.resolve()
        _contained_relative(root, resolved, path)
        manifests.add(resolved)
    return tuple(sorted(manifests | {directory}))


def _relative_to(root: pathlib.Path, path: pathlib.Path) -> str:
    """Return ``path`` as a repository-relative POSIX path.

    A relative path is read as repository-relative rather than as relative to
    the working directory, so the answer does not depend on where the command
    was started: ``--repo-root R src/lib.rs`` names ``R/src/lib.rs`` from any
    directory.  Containment is still checked downstream, so a relative path
    that climbs out of the root is refused there.
    """
    if not path.is_absolute():
        return pathlib.PurePosixPath(path.as_posix()).as_posix()
    try:
        path.resolve().relative_to(root)
    except ValueError as error:
        msg = f"{path} is outside the repository root {root}"
        raise TrustAuditError(msg) from error
    try:
        # Keep the spelling the caller supplied; the module walk resolves its
        # children from that spelling's directory, exactly as rustc does.
        return path.relative_to(root).as_posix()
    except ValueError as error:
        # The path names an in-root file only through a link outside the root.
        # Substituting the resolved spelling would resolve children from the
        # target's directory while the compiler uses the supplied one.
        msg = (
            f"{path} names a file inside {root} only through a symbolic link, "
            "so its module directory cannot be derived from the spelling"
        )
        raise TrustAuditError(msg) from error
