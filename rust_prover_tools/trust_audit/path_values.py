"""Validate and normalise a ``#[path]`` value before path arithmetic.

rustc reads a path value with the host's rules.  A Windows backslash separates
components there and is an ordinary filename character on POSIX; an absolute,
drive-qualified or rooted value names a file outside the declaring module's
tree.  Joining such a value as if it were relative could reach a different
repository file, so it is refused before any join.
"""

from __future__ import annotations

import os
import pathlib
import typing as typ

from rust_prover_tools.trust_audit.errors import TrustAuditError

if typ.TYPE_CHECKING:
    from rust_prover_tools.trust_audit.model import ModuleReference

__all__ = ("portable", "refuse_rooted", "refuse_rooted_paths")

#: A Windows drive prefix is a letter and a colon.
_DRIVE_PREFIX_LENGTH = 2


def is_windows() -> bool:
    r"""Report whether the host reading a ``#[path]`` treats ``\`` as a separator."""
    return os.name == "nt"


def portable(value: str) -> str:
    """Return a ``#[path]`` value in the separator form the host resolves.

    rustc applies the host's path rules, so a Windows backslash separates
    components there while a POSIX backslash is part of the filename.
    Normalising once, before the owner and directory are derived, keeps a
    child's search directory consistent with the file the lookup found.
    """
    return value.replace("\\", "/") if is_windows() else value


def refuse_rooted_paths(reference: ModuleReference) -> None:
    """Refuse a rooted ``#[path]`` on a declaration or any inline module."""
    if reference.path is not None:
        refuse_rooted(reference.path, f"module {reference.name!r}")
    for inline in reference.enclosing:
        if inline.path is not None:
            refuse_rooted(inline.path, f"inline module {inline.name!r}")


def refuse_rooted(value: str, subject: str) -> None:
    """Refuse one absolute, drive-qualified or rooted path value."""
    normalised = portable(value)
    if pathlib.PurePosixPath(normalised).is_absolute():
        msg = f"the #[path] value of {subject} is absolute: {value!r}"
        raise TrustAuditError(msg)
    if _is_drive_qualified(normalised):
        msg = f"the #[path] value of {subject} is drive-qualified: {value!r}"
        raise TrustAuditError(msg)


def _is_drive_qualified(value: str) -> bool:
    """Report whether ``value`` begins with a Windows drive letter."""
    prefix = value[:_DRIVE_PREFIX_LENGTH]
    return (
        len(prefix) == _DRIVE_PREFIX_LENGTH and prefix[0].isalpha() and prefix[1] == ":"
    )
