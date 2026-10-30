"""Resolve optional CLI paths against a repository root."""

from __future__ import annotations

import typing as typ

if typ.TYPE_CHECKING:
    from pathlib import Path

__all__ = ("resolve_configured_path",)


def resolve_configured_path(
    configured: Path | None,
    repo_root: Path,
    default: Path,
) -> Path:
    """Return a configured path, resolving relative values below the root.

    Parameters
    ----------
    configured : Path | None
        User-supplied path, or `None` to use the default.
    repo_root : Path
        Repository root used as the base for relative paths.
    default : Path
        Path used when `configured` is `None`.

    Returns
    -------
    Path
        The selected path with a leading home directory expansion.
    """
    if configured is None:
        return default
    path = configured.expanduser()
    if path.is_absolute():
        return path
    return repo_root / path
