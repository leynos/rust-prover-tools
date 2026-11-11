"""Contained paths and planned links below an extraction root.

A member's path is resolved lexically before it is joined to the staging root,
and a link is created only after its parent directory exists. Both rules are
shared by every extraction pass, so they live here rather than inside the
materialization orchestration.
"""

from __future__ import annotations

import os
import typing as typ

from rust_prover_tools.errors import ProverToolError

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

    from rust_prover_tools.kani_snapshot.archive_graph import ArchiveEntry

__all__ = (
    "DIRECTORY_MODE",
    "create_hard_links",
    "create_symbolic_links",
    "target_path",
)

#: Mode for directories extraction creates before archive modes are applied.
DIRECTORY_MODE: typ.Final[int] = 0o755


def create_symbolic_links(entries: cabc.Sequence[ArchiveEntry], root: Path) -> None:
    """Create planned symbolic links below `root`."""
    for entry in entries:
        target = target_path(root, entry.path)
        link_target = entry.link_target
        if link_target is None:
            msg = f"snapshot symbolic link '{entry.path}' has no target"
            raise ProverToolError(msg)
        try:
            # The graph permits a link below a directory the archive never
            # lists, so the parent has to be created before the link.
            target.parent.mkdir(mode=DIRECTORY_MODE, parents=True, exist_ok=True)
            target.symlink_to(link_target)
        except OSError as exc:
            msg = f"failed to create snapshot symbolic link '{entry.path}': {exc}"
            raise ProverToolError(msg) from exc


def create_hard_links(entries: cabc.Sequence[ArchiveEntry], root: Path) -> None:
    """Create planned hard links to files materialized earlier."""
    for entry in entries:
        source = target_path(root, entry.link_target or "")
        target = target_path(root, entry.path)
        try:
            # Hard-link parents are implicit for the same reason as symbolic
            # ones: only the members themselves need to be listed.
            target.parent.mkdir(mode=DIRECTORY_MODE, parents=True, exist_ok=True)
            os.link(source, target, follow_symlinks=False)
        except OSError as exc:
            msg = f"failed to create snapshot hard link '{entry.path}': {exc}"
            raise ProverToolError(msg) from exc


def target_path(root: Path, relative: str) -> Path:
    """Return a lexically contained path below `root`.

    The graph validator already refuses `..`, absolute and empty components,
    and a member may not sit below a planned link, so no component can redirect
    the join. A lexical check keeps that guarantee cheap for large archives.
    """
    parts = relative.split("/")
    if not _is_contained_path(relative, parts):
        msg = f"snapshot archive path escapes the extraction root: {relative!r}"
        raise ProverToolError(msg)
    return root.joinpath(*parts)


def _is_contained_path(relative: str, parts: cabc.Sequence[str]) -> bool:
    """Return whether every path component is a plain relative name."""
    if not relative or relative.startswith("/"):
        return False
    return ".." not in parts and "" not in parts
