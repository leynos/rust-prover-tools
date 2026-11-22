"""The set of shared objects one toolchain exposes, defined once.

The producer records the toolchain's shared objects in the manifest and the
consumer refuses to install a toolchain that carries one the manifest does not
record. Both sides must therefore select exactly the same files: a producer
that recorded less would publish a snapshot no consumer accepts, and the gap
would only appear after the build had run.

The naming rule and the walk live here so neither side can drift. The walk
follows directory links, visits each real directory once so a link cycle
terminates, and refuses a link that resolves outside the toolchain, because a
shared object reached through it would be hashed from outside the tree the
manifest claims to describe.
"""

from __future__ import annotations

import os
import typing as typ

from rust_prover_tools.errors import ProverToolError

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

__all__ = ("LIBRARY_DIRECTORY", "is_shared_library", "shared_libraries")

#: Directory below a toolchain root whose shared objects are loadable.
LIBRARY_DIRECTORY: typ.Final[str] = "lib"


def is_shared_library(name: str) -> bool:
    """Return whether a file name denotes an ELF shared object.

    Parameters
    ----------
    name : str
        Base name of one directory entry.

    Returns
    -------
    bool
        Whether the name is an unversioned or versioned shared object.
    """
    return name.endswith(".so") or ".so." in name


def _resolve(path: Path, description: str) -> Path:
    """Resolve one path, reporting an unreadable path or a link cycle."""
    try:
        return path.resolve()
    except (OSError, RuntimeError) as exc:
        msg = f"failed to resolve toolchain {description} '{path}': {exc}"
        raise ProverToolError(msg) from exc


def _entries(directory: Path) -> list[os.DirEntry[str]]:
    """Return one directory's entries in name order, or report a failure.

    `os.scandir` yields entries in whatever order the filesystem happens to
    hold them, which differs between machines and between two copies of the
    same toolchain. Sorting makes the walk reproducible; the recorded paths are
    canonical, so even the order a link is met in cannot change the result.
    """
    try:
        with os.scandir(directory) as children:
            return sorted(children, key=lambda child: child.name)
    except OSError as exc:
        msg = f"failed to inspect toolchain library directory: {exc}"
        raise ProverToolError(msg) from exc


def _is_directory(path: Path) -> bool:
    """Return whether `path` is a directory, reporting an inspection failure."""
    try:
        return path.is_dir()
    except OSError as exc:
        msg = f"failed to inspect toolchain library directory '{path}': {exc}"
        raise ProverToolError(msg) from exc


class _LibraryWalk:
    """Collect the shared objects below one resolved toolchain root.

    Holding the traversal state on an object keeps the recursive step to one
    argument and lets the root and the progress callback be threaded through
    without being repeated at every call site.
    """

    def __init__(
        self,
        root: Path,
        resolved_root: Path,
        progress: cabc.Callable[[], None] | None,
    ) -> None:
        """Record the toolchain root, its resolution and the work callback."""
        self._root = root
        self._resolved_root = resolved_root
        self._progress = progress
        self._visited: set[Path] = set()
        self.found: set[str] = set()

    def walk(self, directory: Path) -> None:
        """Collect the shared objects below one directory into `found`.

        The walk descends into the resolved directory, never the spelling it
        was reached by. An aliased directory reached before its real name would
        otherwise record every file inside it twice, once under each spelling,
        and the recorded set would depend on the order the filesystem happens
        to return. Resolving the directory keeps the file names below it, which
        is what makes a library alias part of the recorded identity.
        """
        self._tick()
        resolved = self._resolve_inside(directory, "library directory")
        if not _is_directory(resolved):
            return
        if resolved in self._visited:
            # A link cycle is harmless once its real directory has been walked.
            return
        self._visited.add(resolved)
        for child in _entries(resolved):
            self._tick()
            self._visit(resolved / child.name, child)

    def _visit(self, path: Path, child: os.DirEntry[str]) -> None:
        """Descend into a directory, or record one shared object.

        `DirEntry.is_dir` and `is_file` can raise `OSError` for an entry whose
        link target cannot be inspected, such as a link to itself. Reporting it
        with the path keeps the module's error contract intact.
        """
        try:
            is_directory = child.is_dir(follow_symlinks=True)
            is_file = child.is_file(follow_symlinks=True)
        except OSError as exc:
            msg = f"failed to inspect toolchain entry '{path}': {exc}"
            raise ProverToolError(msg) from exc
        if is_directory:
            self.walk(path)
        elif is_file and is_shared_library(child.name):
            self._record(path)

    def _record(self, path: Path) -> None:
        """Record one shared object by the path the toolchain exposes it under.

        The path is relative to the resolved toolchain and its directory
        spelling is canonical, because `walk` only descends into resolved
        directories. The entry's own name is kept even when it is a link, so a
        library alias is part of the recorded identity: retargeting or
        removing `lib/libdriver.so` then changes what the manifest records and
        the consumer refuses the altered toolchain.
        """
        self._resolve_inside(path, "library")
        self.found.add(path.relative_to(self._resolved_root).as_posix())

    def _resolve_inside(self, path: Path, description: str) -> Path:
        """Resolve `path`, refusing one that leaves the toolchain."""
        resolved = _resolve(path, description)
        if not resolved.is_relative_to(self._resolved_root):
            msg = f"toolchain {description} escapes its toolchain: {path}"
            raise ProverToolError(msg)
        return resolved

    def _tick(self) -> None:
        """Account for one inspected entry, when the caller asked to."""
        if self._progress is not None:
            self._progress()


def shared_libraries(
    root: Path,
    progress: cabc.Callable[[], None] | None = None,
) -> tuple[str, ...]:
    """Return the toolchain-relative path of every shared object below `lib`.

    Parameters
    ----------
    root : Path
        Toolchain root, the directory that holds `lib`.
    progress : collections.abc.Callable[[], None] | None
        Optional callback invoked once per inspected entry, so a caller with a
        work budget can account for the traversal.

    Returns
    -------
    tuple[str, ...]
        Sorted POSIX paths relative to `root`, so a recorded digest and a
        checked file share one spelling.

    Raises
    ------
    ProverToolError
        Raised when the directory tree cannot be inspected, or when a link in
        it resolves outside the toolchain.
    """
    walk = _LibraryWalk(root, _resolve(root, "root"), progress)
    walk.walk(root / LIBRARY_DIRECTORY)
    return tuple(sorted(walk.found))
