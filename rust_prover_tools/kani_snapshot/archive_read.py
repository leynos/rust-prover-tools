"""Header scanning and validation for snapshot tar archives.

Reading is separate from materialization so the complete effective member list,
including every link target, is checked before any byte is written. The path
and link-graph rules themselves live in
`rust_prover_tools.kani_snapshot.archive_graph`.
"""

from __future__ import annotations

import contextlib
import tarfile
import typing as typ

from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_snapshot.archive_graph import (
    DEFAULT_ARCHIVE_LIMITS,
    DEFAULT_ARCHIVE_POLICY,
    KIND_DIRECTORY,
    KIND_FILE,
    KIND_HARDLINK,
    KIND_SYMLINK,
    ArchiveEntry,
    ArchiveLimits,
    ArchivePolicy,
    normalize_member_path,
    validate_graph,
)
from rust_prover_tools.kani_snapshot.progress import Progress, as_checkpoint

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

__all__ = ("open_tar_archive", "read_archive")

_PRIVILEGED_MODE_BITS: typ.Final[int] = 0o7000
_LINK_KINDS: typ.Final[frozenset[str]] = frozenset({KIND_SYMLINK, KIND_HARDLINK})


class _CheckpointedSource:
    """Raw file wrapper that reports the caller's progress on every read.

    `tarfile` reaches the next header by reading through the compressed stream,
    so skipping one large member can outlive a caller's deadline even though a
    checkpoint runs after each header. Reporting on the raw reads bounds that
    window instead of leaving it between two headers.
    """

    def __init__(self, raw: typ.BinaryIO, progress: Progress) -> None:
        """Wrap `raw`, reporting `progress` before each read."""
        self._raw = raw
        self._progress = progress

    def read(self, size: int = -1) -> bytes:
        """Read up to `size` bytes after reporting progress."""
        self._progress()
        return self._raw.read(size)

    def seekable(self) -> bool:
        """Report whether the wrapped file supports seeking."""
        return self._raw.seekable()

    def seek(self, offset: int, whence: int = 0) -> int:
        """Seek the wrapped file."""
        return self._raw.seek(offset, whence)

    def tell(self) -> int:
        """Return the wrapped file's position."""
        return self._raw.tell()


@contextlib.contextmanager
def open_tar_archive(
    archive_path: Path,
    checkpoint: Progress | None = None,
) -> cabc.Generator[tarfile.TarFile]:
    """Open a gzip tar archive whose reads report the caller's progress.

    Parameters
    ----------
    archive_path : Path
        Gzip tar archive to open.
    checkpoint : Progress | None
        Called on every raw read, so a scan that skips a large member's body
        still reaches a deadline. `None` is a no-op.

    Yields
    ------
    tarfile.TarFile
        Open archive. The caller owns the iteration and must not close it.

    Raises
    ------
    OSError
        Raised when the archive cannot be opened for reading.
    tarfile.TarError
        Raised when the bytes are not a valid gzip tar stream.
    """
    progress = as_checkpoint(checkpoint)
    with archive_path.open("rb") as raw:
        # `tarfile` only needs the read, seek and tell subset this wrapper
        # provides; the cast records that narrower surface at the boundary.
        source = typ.cast("typ.IO[bytes]", _CheckpointedSource(raw, progress))
        with tarfile.open(None, mode="r:gz", fileobj=source) as archive:
            yield archive


def read_archive(
    archive_path: Path,
    *,
    limits: ArchiveLimits = DEFAULT_ARCHIVE_LIMITS,
    policy: ArchivePolicy = DEFAULT_ARCHIVE_POLICY,
    checkpoint: Progress | None = None,
) -> tuple[ArchiveEntry, ...]:
    """Validate an archive's complete member graph without extracting it.

    Parameters
    ----------
    archive_path : Path
        Gzip tar archive to inspect.
    limits : ArchiveLimits
        Member, size, path and link ceilings.
    policy : ArchivePolicy
        Component stripping and optional required root.
    checkpoint : Progress | None
        Called for each member and on every raw read, so a caller's deadline
        interrupts a large archive while it is still being scanned, including
        while a large member's body is skipped. `None` is a no-op.

    Returns
    -------
    tuple[ArchiveEntry, ...]
        Validated members in header order.

    Raises
    ------
    ProverToolError
        Raised for malformed archives or any member outside the containment,
        mode, link or resource policy.
    """
    progress = as_checkpoint(checkpoint)
    entries: list[ArchiveEntry] = []
    total = 0
    try:
        with open_tar_archive(archive_path, progress) as archive:
            for count, member in enumerate(archive, start=1):
                progress()
                if count > limits.max_members:
                    msg = f"snapshot archive has more than {limits.max_members} members"
                    raise ProverToolError(msg)
                entry = _entry_from_member(member, limits, policy)
                if entry is None:
                    continue
                total += entry.size
                if total > limits.max_total_bytes:
                    msg = (
                        "snapshot archive declares more than "
                        f"{limits.max_total_bytes} bytes of extracted data"
                    )
                    raise ProverToolError(msg)
                entries.append(entry)
    except ValueError as exc:
        # tarfile raises ValueError while parsing a malformed GNU sparse map,
        # so this also covers a hostile sparse header that never reaches us.
        msg = f"snapshot archive has an invalid or sparse member: {exc}"
        raise ProverToolError(msg) from exc
    except (tarfile.TarError, OSError, EOFError) as exc:
        msg = f"snapshot archive is not a valid gzip tar file: {exc}"
        raise ProverToolError(msg) from exc
    validate_graph(entries, limits)
    if not any(entry.kind == KIND_FILE for entry in entries):
        msg = "snapshot archive contains no regular files"
        raise ProverToolError(msg)
    return tuple(entries)


def _entry_from_member(
    member: tarfile.TarInfo,
    limits: ArchiveLimits,
    policy: ArchivePolicy,
) -> ArchiveEntry | None:
    """Validate one header and return its effective entry, or skip a root."""
    if member.issparse() or any(
        key.startswith("GNU.sparse.") for key in member.pax_headers
    ):
        msg = f"snapshot archive member is sparse: {member.name!r}"
        raise ProverToolError(msg)
    if member.mode & _PRIVILEGED_MODE_BITS:
        msg = f"snapshot archive member has a privileged mode: {member.name!r}"
        raise ProverToolError(msg)
    kind = _member_kind(member)
    normalized = normalize_member_path(member.name, max_length=limits.max_path_length)
    path = _apply_policy(normalized, kind, policy)
    if path is None:
        return None
    size = member.size if kind == KIND_FILE else 0
    target = _effective_link_target(member, kind, limits, policy)
    return ArchiveEntry(
        path=path,
        kind=kind,
        mode=member.mode & 0o777,
        size=size,
        link_target=target,
    )


def _member_kind(member: tarfile.TarInfo) -> str:
    """Classify a header, rejecting special and unknown member types."""
    if member.isdir():
        return KIND_DIRECTORY
    if member.issym():
        return KIND_SYMLINK
    if member.islnk():
        return KIND_HARDLINK
    if member.isreg():
        return KIND_FILE
    msg = f"snapshot archive member is a special file: {member.name!r}"
    raise ProverToolError(msg)


def _apply_policy(
    path: str,
    kind: str,
    policy: ArchivePolicy,
) -> str | None:
    """Strip policy components, requiring the configured root."""
    parts = path.split("/")
    if policy.required_root is not None and parts[0] != policy.required_root:
        msg = (
            f"snapshot archive member is outside required root "
            f"'{policy.required_root}': {path!r}"
        )
        raise ProverToolError(msg)
    if len(parts) <= policy.strip_components:
        # GNU tar ignores the stripped root directory entry itself.
        if kind == KIND_DIRECTORY and len(parts) == policy.strip_components:
            return None
        msg = f"snapshot archive member path is too short to strip: {path!r}"
        raise ProverToolError(msg)
    return "/".join(parts[policy.strip_components :])


def _effective_link_target(
    member: tarfile.TarInfo,
    kind: str,
    limits: ArchiveLimits,
    policy: ArchivePolicy,
) -> str | None:
    """Return a bounded symbolic or stripped hard-link target."""
    if kind not in _LINK_KINDS:
        return None
    target = member.linkname
    if not target:
        msg = f"snapshot archive link has no target: {member.name!r}"
        raise ProverToolError(msg)
    if len(target.encode("utf-8")) > limits.max_path_length:
        msg = f"snapshot archive link target exceeds {limits.max_path_length} bytes"
        raise ProverToolError(msg)
    if kind == KIND_SYMLINK:
        return target
    is_unsafe = (
        target.startswith("/")
        or ".." in target.split("/")
        or "\\" in target
        or "\x00" in target
    )
    if is_unsafe:
        msg = f"snapshot archive hard link has an unsafe target: {target!r}"
        raise ProverToolError(msg)
    try:
        normalized = normalize_member_path(target, max_length=limits.max_path_length)
    except ProverToolError as exc:
        msg = f"snapshot archive hard link target is invalid: {target!r}: {exc}"
        raise ProverToolError(msg) from exc
    stripped = _apply_policy(normalized, KIND_FILE, policy)
    if stripped is None:
        msg = "snapshot archive hard link target is empty after stripping"
        raise ProverToolError(msg)
    return stripped
