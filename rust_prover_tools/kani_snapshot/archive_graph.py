"""Path, policy and link-graph validation for snapshot tar archives.

These checks decide what an archive is allowed to *name*. They run over the
complete member list before any byte is materialized, so extraction never has
to discover an escaping path or a dangling link part-way through writing.
"""

from __future__ import annotations

import dataclasses as dc
import posixpath
import typing as typ

from rust_prover_tools.errors import ProverToolError

if typ.TYPE_CHECKING:
    import collections.abc as cabc

__all__ = (
    "DEFAULT_ARCHIVE_LIMITS",
    "DEFAULT_ARCHIVE_POLICY",
    "KIND_DIRECTORY",
    "KIND_FILE",
    "KIND_HARDLINK",
    "KIND_SYMLINK",
    "ArchiveEntry",
    "ArchiveLimits",
    "ArchivePolicy",
    "normalize_member_path",
    "validate_graph",
)

KIND_FILE: typ.Final[str] = "file"
KIND_DIRECTORY: typ.Final[str] = "directory"
KIND_SYMLINK: typ.Final[str] = "symlink"
KIND_HARDLINK: typ.Final[str] = "hardlink"

_DEFAULT_MAX_MEMBERS: typ.Final[int] = 100_000
_DEFAULT_MAX_TOTAL_BYTES: typ.Final[int] = 8 * 1024**3
_DEFAULT_MAX_PATH_LENGTH: typ.Final[int] = 4096
_DEFAULT_MAX_LINK_HOPS: typ.Final[int] = 16


@dc.dataclass(frozen=True, slots=True)
class ArchiveLimits:
    """Resource ceilings applied while reading and extracting an archive.

    Attributes
    ----------
    max_members : int
        Maximum member headers. Defaults to 100,000.
    max_total_bytes : int
        Maximum summed declared file sizes. Defaults to 8 GiB.
    max_path_length : int
        Maximum UTF-8 path length in bytes. Defaults to 4 KiB.
    max_link_hops : int
        Maximum symbolic-link hops. Defaults to 16.
    """

    max_members: int = _DEFAULT_MAX_MEMBERS
    max_total_bytes: int = _DEFAULT_MAX_TOTAL_BYTES
    max_path_length: int = _DEFAULT_MAX_PATH_LENGTH
    max_link_hops: int = _DEFAULT_MAX_LINK_HOPS

    def __post_init__(self) -> None:
        """Reject limits that cannot bound the work."""
        for name in ("max_members", "max_total_bytes", "max_path_length"):
            if getattr(self, name) < 1:
                msg = f"archive limit {name} must be positive"
                raise ProverToolError(msg)
        if self.max_link_hops < 0:
            msg = "archive limit max_link_hops must not be negative"
            raise ProverToolError(msg)


@dc.dataclass(frozen=True, slots=True)
class ArchivePolicy:
    """How one archive's paths map into the extraction root.

    Attributes
    ----------
    strip_components : int
        Leading path components removed from every member. Defaults to zero.
    required_root : str | None
        Optional single root component every member must share. Requires
        `strip_components` to be one.
    """

    strip_components: int = 0
    required_root: str | None = None

    def __post_init__(self) -> None:
        """Reject an ambiguous strip/root combination."""
        if self.strip_components < 0:
            msg = "archive strip_components must not be negative"
            raise ProverToolError(msg)
        if self.required_root is None:
            return
        if self.strip_components != 1:
            msg = "archive required_root requires strip_components of one"
            raise ProverToolError(msg)
        if (
            normalize_member_path(self.required_root, max_length=4096)
            != self.required_root
            or "/" in self.required_root
        ):
            msg = (
                "archive required_root is not one safe component: "
                f"'{self.required_root}'"
            )
            raise ProverToolError(msg)


@dc.dataclass(frozen=True, slots=True)
class ArchiveEntry:
    """One validated archive member after policy mapping.

    Attributes
    ----------
    path : str
        Effective extraction-relative path.
    kind : str
        One of `KIND_FILE`, `KIND_DIRECTORY`, `KIND_SYMLINK` or `KIND_HARDLINK`.
    mode : int
        Ordinary permission bits without setuid, setgid or sticky bits.
    size : int
        Declared file size, zero for links and directories.
    link_target : str | None
        Effective symbolic or archive-root-relative hard-link target.
    """

    path: str
    kind: str
    mode: int
    size: int
    link_target: str | None = None


DEFAULT_ARCHIVE_LIMITS: typ.Final[ArchiveLimits] = ArchiveLimits()
DEFAULT_ARCHIVE_POLICY: typ.Final[ArchivePolicy] = ArchivePolicy()


def normalize_member_path(name: str, *, max_length: int) -> str:
    """Return a normalized relative path or reject an unsafe one.

    Parameters
    ----------
    name : str
        Effective member name from the archive header.
    max_length : int
        Maximum accepted UTF-8 length in bytes.

    Returns
    -------
    str
        Path with `.` and empty components removed.

    Raises
    ------
    ProverToolError
        Raised for an empty name, NUL or backslash, an absolute path, a drive
        prefix, a parent component, or an over-long path.
    """
    if not name:
        msg = "archive member path is empty"
        raise ProverToolError(msg)
    if "\x00" in name or "\\" in name:
        msg = f"archive member path contains a forbidden character: {name!r}"
        raise ProverToolError(msg)
    if name.startswith("/"):
        msg = f"archive member path is absolute: {name!r}"
        raise ProverToolError(msg)
    parts = [part for part in name.split("/") if part not in {"", "."}]
    if not parts:
        msg = f"archive member path has no components: {name!r}"
        raise ProverToolError(msg)
    if ".." in parts:
        msg = f"archive member path traverses its parent: {name!r}"
        raise ProverToolError(msg)
    if _has_drive_prefix(parts[0]):
        msg = f"archive member path has a drive prefix: {name!r}"
        raise ProverToolError(msg)
    normalized = "/".join(parts)
    if len(normalized.encode("utf-8")) > max_length:
        msg = f"archive member path exceeds {max_length} bytes: {name!r}"
        raise ProverToolError(msg)
    return normalized


def _has_drive_prefix(component: str) -> bool:
    """Return whether a first component looks like `C:`."""
    return bool(component[:1].isalpha() and component[1:2] == ":")


def validate_graph(entries: cabc.Sequence[ArchiveEntry], limits: ArchiveLimits) -> None:
    """Reject duplicate paths, collisions and unsafe link graphs.

    Parameters
    ----------
    entries : Sequence[ArchiveEntry]
        Members already mapped by the archive policy.
    limits : ArchiveLimits
        Link-hop ceiling.

    Raises
    ------
    ProverToolError
        Raised for a duplicate path, a member below a non-directory, a link
        escape, a dangling link or a link cycle.
    """
    kinds = _collect_kinds(entries)
    directories = _directory_set(kinds)
    _validate_ancestors(entries, kinds)
    index = {entry.path: entry for entry in entries}
    for entry in entries:
        if entry.kind == KIND_SYMLINK:
            _validate_symlink(entry, index, directories, limits)
        elif entry.kind == KIND_HARDLINK:
            _validate_hardlink(entry, kinds)


def _collect_kinds(entries: cabc.Sequence[ArchiveEntry]) -> dict[str, str]:
    """Map each path to its kind, rejecting a repeated effective path."""
    kinds: dict[str, str] = {}
    for entry in entries:
        if entry.path in kinds:
            msg = f"snapshot archive lists '{entry.path}' more than once"
            raise ProverToolError(msg)
        kinds[entry.path] = entry.kind
    return kinds


def _validate_ancestors(
    entries: cabc.Sequence[ArchiveEntry], kinds: cabc.Mapping[str, str]
) -> None:
    """Reject a member that sits below a planned non-directory."""
    for entry in entries:
        for ancestor in _ancestors(entry.path):
            ancestor_kind = kinds.get(ancestor)
            if ancestor_kind is not None and ancestor_kind != KIND_DIRECTORY:
                msg = (
                    f"snapshot archive member '{entry.path}' collides with "
                    f"non-directory '{ancestor}'"
                )
                raise ProverToolError(msg)


def _directory_set(kinds: cabc.Mapping[str, str]) -> frozenset[str]:
    """Return explicit and implicit directory paths."""
    directories = {path for path, kind in kinds.items() if kind == KIND_DIRECTORY}
    for path in kinds:
        directories.update(_ancestors(path))
    return frozenset(directories)


def _ancestors(path: str) -> tuple[str, ...]:
    """Return every proper ancestor prefix of `path`."""
    parts = path.split("/")
    return tuple("/".join(parts[:index]) for index in range(1, len(parts)))


def _validate_symlink(
    entry: ArchiveEntry,
    index: cabc.Mapping[str, ArchiveEntry],
    directories: frozenset[str],
    limits: ArchiveLimits,
) -> None:
    """Resolve a symbolic link's chain inside the graph, without cycles."""
    current = entry
    visited = {entry.path}
    for _ in range(1, limits.max_link_hops + 1):
        resolved = _resolve_relative(current.path, current.link_target)
        next_entry = index.get(resolved)
        if next_entry is not None and next_entry.kind == KIND_SYMLINK:
            if resolved in visited:
                msg = f"snapshot archive symbolic link cycle at '{resolved}'"
                raise ProverToolError(msg)
            visited.add(resolved)
            current = next_entry
            continue
        if next_entry is not None or resolved in directories:
            return
        msg = f"snapshot archive symbolic link '{entry.path}' is dangling"
        raise ProverToolError(msg)
    msg = (
        f"snapshot archive symbolic link '{entry.path}' exceeds "
        f"{limits.max_link_hops} hops"
    )
    raise ProverToolError(msg)


def _resolve_relative(link_path: str, target: str | None) -> str:
    """Resolve a symbolic target against its directory, refusing escapes."""
    if target is None or _has_forbidden_characters(target):
        msg = f"snapshot archive symbolic link has an invalid target: {link_path!r}"
        raise ProverToolError(msg)
    if target.startswith("/"):
        msg = f"snapshot archive symbolic link '{link_path}' is absolute"
        raise ProverToolError(msg)
    resolved = posixpath.normpath(posixpath.join(posixpath.dirname(link_path), target))
    if resolved in {".", ".."} or resolved.startswith("../"):
        msg = f"snapshot archive symbolic link '{link_path}' escapes the root"
        raise ProverToolError(msg)
    return resolved


def _has_forbidden_characters(text: str) -> bool:
    """Return whether an archive name carries a NUL or backslash."""
    return "\x00" in text or "\\" in text


def _validate_hardlink(entry: ArchiveEntry, kinds: cabc.Mapping[str, str]) -> None:
    """Require a hard link to name a contained regular file."""
    target = entry.link_target or ""
    if target.startswith("/") or ".." in target.split("/"):
        msg = f"snapshot archive hard link '{entry.path}' escapes the root"
        raise ProverToolError(msg)
    if kinds.get(target) != KIND_FILE:
        msg = (
            f"snapshot archive hard link '{entry.path}' does not target "
            "a contained regular file"
        )
        raise ProverToolError(msg)
