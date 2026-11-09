"""Contained materialization of validated snapshot tar archives.

Extraction builds a private sibling directory, writes regular files before any
link, fsyncs content and metadata, and renames the staging tree into place so a
failure never publishes a partial tree. Validation always happens first, via
`rust_prover_tools.kani_snapshot.archive_read`.
"""

from __future__ import annotations

import contextlib
import dataclasses as dc
import logging
import os
import shutil
import tarfile
import typing as typ

from rust_prover_tools.download_stream import READ_CHUNK_SIZE
from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_snapshot.archive_destination import (
    PRIVATE_DIRECTORY_MODE,
    prepare_destination,
)
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
)
from rust_prover_tools.kani_snapshot.archive_read import (
    _entry_from_member,
    open_tar_archive,
    read_archive,
)
from rust_prover_tools.kani_snapshot.progress import Progress, as_checkpoint
from rust_prover_tools.observability import log_event

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

__all__ = ("extract_archive",)

_DIRECTORY_MODE: typ.Final[int] = 0o755
_LINK_KINDS: typ.Final[frozenset[str]] = frozenset({KIND_SYMLINK, KIND_HARDLINK})
_LOGGER = logging.getLogger(__name__)

#: Errors that mean "this is not the tar stream the headers promised".
_TAR_STREAM_ERRORS: typ.Final[tuple[type[BaseException], ...]] = (
    tarfile.TarError,
    EOFError,
)


# pylint: disable-next=too-many-arguments,too-many-positional-arguments
def extract_archive(  # ruff: ignore[too-many-arguments] - one parameter per archive input
    archive_path: Path,
    destination: Path,
    *,
    limits: ArchiveLimits = DEFAULT_ARCHIVE_LIMITS,
    policy: ArchivePolicy = DEFAULT_ARCHIVE_POLICY,
    checkpoint: Progress | None = None,
) -> tuple[ArchiveEntry, ...]:
    """Validate and materialize an archive into an empty destination.

    Parameters
    ----------
    archive_path : Path
        Verified gzip tar archive.
    destination : Path
        Final extraction root. It must not exist, or be an empty directory.
    limits : ArchiveLimits
        Member, size, path and link ceilings.
    policy : ArchivePolicy
        Component stripping and optional required root.
    checkpoint : Progress | None
        Called while headers and member bodies are processed, so a caller's
        deadline bounds extraction rather than only its start and finish.
        `None` is a no-op.

    Returns
    -------
    tuple[ArchiveEntry, ...]
        The validated members that were materialized.

    Raises
    ------
    ProverToolError
        Raised when validation, extraction or publication fails.
    """
    progress = as_checkpoint(checkpoint)
    entries = read_archive(
        archive_path,
        limits=limits,
        policy=policy,
        checkpoint=progress,
    )
    staging = prepare_destination(destination)
    extraction = _Extraction(
        archive_path=archive_path,
        root=staging,
        limits=limits,
        policy=policy,
        checkpoint=progress,
    )
    try:
        staging.mkdir(mode=PRIVATE_DIRECTORY_MODE, parents=False)
        _materialize_files(extraction)
        _materialize_links(extraction)
        _apply_modes(entries, staging, progress)
        _fsync_directory(staging)
        if destination.exists():
            destination.rmdir()
        staging.replace(destination)
        _fsync_directory(destination.parent)
    except (tarfile.TarError, EOFError, ValueError) as exc:
        msg = f"snapshot archive is not a valid gzip tar file: {exc}"
        shutil.rmtree(staging, ignore_errors=True)
        raise ProverToolError(msg) from exc
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    log_event(
        _LOGGER,
        "archive.extracted",
        members=len(entries),
        bytes=sum(entry.size for entry in entries),
    )
    return entries


@contextlib.contextmanager
def _open_archive(
    archive_path: Path,
    checkpoint: Progress,
) -> cabc.Generator[tarfile.TarFile]:
    """Open an archive for a materialization pass, reporting open failures.

    Reads report the caller's checkpoint too, so a pass that only scans
    headers still reaches a deadline while it skips a large member's body. The
    surrounding `try` also turns an unusual mid-stream `OSError` into the same
    archive-level report; member bodies raise `tarfile.TarError`, which the
    caller handles separately.
    """
    try:
        with open_tar_archive(archive_path, checkpoint) as archive:
            yield archive
    except OSError as exc:
        msg = f"failed to read snapshot archive '{archive_path}': {exc}"
        raise ProverToolError(msg) from exc


@dc.dataclass(frozen=True, slots=True)
class _Extraction:
    """The immutable inputs that materialize one archive into a root."""

    archive_path: Path
    root: Path
    limits: ArchiveLimits
    policy: ArchivePolicy
    checkpoint: Progress


def _materialize_files(extraction: _Extraction) -> None:
    """Create directories and regular files, skipping links."""
    total = 0
    with _open_archive(extraction.archive_path, extraction.checkpoint) as archive:
        for member in archive:
            extraction.checkpoint()
            entry = _entry_from_member(member, extraction.limits, extraction.policy)
            if entry is None or entry.kind in _LINK_KINDS:
                continue
            target = _target_path(extraction.root, entry.path)
            if entry.kind == KIND_DIRECTORY:
                target.mkdir(mode=_DIRECTORY_MODE, parents=True, exist_ok=True)
                continue
            target.parent.mkdir(mode=_DIRECTORY_MODE, parents=True, exist_ok=True)
            write = _MemberWrite(
                archive=archive,
                member=member,
                target=target,
                entry=entry,
            )
            total += _copy_member(
                write,
                total,
                extraction.limits,
                extraction.checkpoint,
            )


@dc.dataclass(frozen=True, slots=True)
class _MemberWrite:
    """One regular-file write, grouped to keep the copy helper's arity small."""

    archive: tarfile.TarFile
    member: tarfile.TarInfo
    target: Path
    entry: ArchiveEntry


def _copy_member(
    write: _MemberWrite,
    already_written: int,
    limits: ArchiveLimits,
    checkpoint: Progress,
) -> int:
    """Stream one regular file with descriptor and size enforcement."""
    entry = write.entry
    source = write.archive.extractfile(write.member)
    if source is None:
        msg = f"snapshot archive member '{entry.path}' has no body"
        raise ProverToolError(msg)
    try:
        descriptor = os.open(
            write.target,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
        )
    except OSError as exc:
        msg = f"failed to extract snapshot archive member '{entry.path}': {exc}"
        raise ProverToolError(msg) from exc
    written = 0
    try:
        with os.fdopen(descriptor, "wb") as output, source:
            while chunk := source.read(READ_CHUNK_SIZE):
                checkpoint()
                written += len(chunk)
                if already_written + written > limits.max_total_bytes:
                    msg = "snapshot archive extracted more data than declared"
                    raise ProverToolError(msg)
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
    except OSError as exc:
        msg = f"failed to extract snapshot archive member '{entry.path}': {exc}"
        raise ProverToolError(msg) from exc
    except _TAR_STREAM_ERRORS as exc:
        msg = f"snapshot archive member '{entry.path}' is incomplete: {exc}"
        raise ProverToolError(msg) from exc
    if written != entry.size:
        msg = (
            f"snapshot archive member '{entry.path}' is incomplete: "
            f"received {written} bytes, expected {entry.size}"
        )
        raise ProverToolError(msg)
    return written


def _materialize_links(extraction: _Extraction) -> None:
    """Create symbolic links, then hard links to already written files."""
    symbolic: list[ArchiveEntry] = []
    hard: list[ArchiveEntry] = []
    with _open_archive(extraction.archive_path, extraction.checkpoint) as archive:
        for member in archive:
            extraction.checkpoint()
            entry = _entry_from_member(member, extraction.limits, extraction.policy)
            if entry is None:
                continue
            if entry.kind == KIND_SYMLINK:
                symbolic.append(entry)
            elif entry.kind == KIND_HARDLINK:
                hard.append(entry)
    _create_symbolic_links(symbolic, extraction.root)
    _create_hard_links(hard, extraction.root)


def _create_symbolic_links(entries: cabc.Sequence[ArchiveEntry], root: Path) -> None:
    """Create planned symbolic links below `root`."""
    for entry in entries:
        target = _target_path(root, entry.path)
        link_target = entry.link_target
        if link_target is None:
            msg = f"snapshot symbolic link '{entry.path}' has no target"
            raise ProverToolError(msg)
        try:
            # The graph permits a link below a directory the archive never
            # lists, so the parent has to be created before the link.
            target.parent.mkdir(mode=_DIRECTORY_MODE, parents=True, exist_ok=True)
            target.symlink_to(link_target)
        except OSError as exc:
            msg = f"failed to create snapshot symbolic link '{entry.path}': {exc}"
            raise ProverToolError(msg) from exc


def _create_hard_links(entries: cabc.Sequence[ArchiveEntry], root: Path) -> None:
    """Create planned hard links to files materialized earlier."""
    for entry in entries:
        source = _target_path(root, entry.link_target or "")
        target = _target_path(root, entry.path)
        try:
            # Hard-link parents are implicit for the same reason as symbolic
            # ones: only the members themselves need to be listed.
            target.parent.mkdir(mode=_DIRECTORY_MODE, parents=True, exist_ok=True)
            os.link(source, target, follow_symlinks=False)
        except OSError as exc:
            msg = f"failed to create snapshot hard link '{entry.path}': {exc}"
            raise ProverToolError(msg) from exc


def _target_path(root: Path, relative: str) -> Path:
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


def _apply_modes(
    entries: cabc.Sequence[ArchiveEntry],
    root: Path,
    checkpoint: Progress,
) -> None:
    """Apply ordinary permission bits to files, then deepest directories.

    Files are opened while still private and mode 0600, so even a final mode of
    0000 can be set and flushed. Hard links share their target's inode and
    symbolic links have no independent mode, so both are skipped.
    """
    files = [entry for entry in entries if entry.kind == KIND_FILE]
    directories = sorted(
        (entry for entry in entries if entry.kind == KIND_DIRECTORY),
        key=lambda entry: entry.path.count("/"),
        reverse=True,
    )
    for entry in (*files, *directories):
        checkpoint()
        _apply_mode(root, entry, is_directory=entry.kind == KIND_DIRECTORY)


def _apply_mode(root: Path, entry: ArchiveEntry, *, is_directory: bool) -> None:
    """Set one entry's mode through an already-open descriptor."""
    target = _target_path(root, entry.path)
    flags = os.O_RDONLY | os.O_NOFOLLOW | (os.O_DIRECTORY if is_directory else 0)
    try:
        descriptor = os.open(target, flags)
    except OSError as exc:
        msg = f"failed to open snapshot archive member '{entry.path}': {exc}"
        raise ProverToolError(msg) from exc
    try:
        os.fchmod(descriptor, entry.mode)
        os.fsync(descriptor)
    except OSError as exc:
        msg = f"failed to set snapshot archive mode for '{entry.path}': {exc}"
        raise ProverToolError(msg) from exc
    finally:
        os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    """Flush directory entries so a rename is durable."""
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
