"""Extraction and publication of the prebuilt Kani frontend executables.

The frontend archive holds `cargo-kani` and `kani`. They are extracted to
pending files so a truncated copy is never runnable, then published together:
an existing executable is moved aside first, and a failure in the second
publication restores the first so the install directory keeps one consistent
set. Cleanup never hides the failure that caused it, and a cleanup failure on
its own fails the extraction.
"""

from __future__ import annotations

import logging
import tarfile
import typing as typ

from rust_prover_tools.binary_assets import (
    COPY_CHUNK_SIZE,
    KANI_FRONTEND_ASSET,
    KANI_FRONTEND_MEMBER_MAX_BYTES,
    KANI_FRONTEND_MEMBERS,
)
from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.observability import log_event

if typ.TYPE_CHECKING:
    from pathlib import Path

__all__ = ("extract_frontend",)

_LOGGER = logging.getLogger(__name__)


def extract_frontend(archive_path: Path, install_dir: Path) -> int:
    """Extract the two Kani frontend executables and return the bytes written."""
    pending: list[tuple[Path, Path]] = []
    try:
        written = _extract_members(archive_path, install_dir, pending)
    except BaseException:
        # A cleanup failure must not replace the extraction failure that caused
        # it, and every registered file still gets its own removal attempt.
        _discard_pending(pending, has_prior_failure=True)
        raise
    _discard_pending(pending, has_prior_failure=False)
    log_event(
        _LOGGER,
        "kani.extract.complete",
        asset=KANI_FRONTEND_ASSET.name,
        members=len(KANI_FRONTEND_MEMBERS),
        bytes=written,
    )
    return written


def _extract_members(
    archive_path: Path,
    install_dir: Path,
    pending: list[tuple[Path, Path]],
) -> int:
    """Extract and publish both executables, registering each pending file."""
    written = 0
    try:
        install_dir.mkdir(parents=True, exist_ok=True)
        with tarfile.open(archive_path, "r:gz") as archive:
            for name in KANI_FRONTEND_MEMBERS:
                member = _regular_member(archive, name)
                temporary = install_dir / f".{name}.download"
                # Register before writing so a partial copy is still removed.
                pending.append((temporary, install_dir / name))
                written += _copy_member(archive, member, temporary)
            _publish_frontend(pending)
    except (OSError, tarfile.TarError) as exc:
        msg = f"failed to extract prebuilt Kani archive '{archive_path}': {exc}"
        raise ProverToolError(msg) from exc
    return written


def _discard_pending(
    pending: list[tuple[Path, Path]],
    *,
    has_prior_failure: bool,
) -> None:
    """Remove every pending file, reporting failures at the right level."""
    first_failed: Path | None = None
    first_error: OSError | None = None
    for temporary, _ in pending:
        try:
            temporary.unlink(missing_ok=True)
        except OSError as exc:
            log_event(_LOGGER, "kani.cleanup_failed", reason="cleanup_error")
            if first_failed is None:
                first_failed = temporary
                first_error = exc
    if first_failed is None or first_error is None:
        return
    if has_prior_failure:
        return
    msg = f"failed to remove pending Kani file '{first_failed}': {first_error}"
    raise ProverToolError(msg) from first_error


def _publish_frontend(pending: list[tuple[Path, Path]]) -> None:
    """Publish both executables together, rolling back a partial replacement."""
    published: list[tuple[Path, Path | None]] = []
    try:
        for temporary, target in pending:
            backup = _move_original_aside(target)
            try:
                temporary.replace(target)
            except OSError:
                # A failed replace leaves the destination untouched, so only
                # this member's move aside must be undone here.
                _restore_backup(target, backup)
                raise
            published.append((target, backup))
    except OSError:
        _roll_back(published)
        raise
    for _, backup in published:
        if backup is not None:
            _discard_backup(backup)


def _move_original_aside(target: Path) -> Path | None:
    """Move an existing executable to a backup path, or report none existed."""
    if not target.exists():
        return None
    backup = target.with_name(f".{target.name}.previous")
    target.replace(backup)
    return backup


def _restore_backup(target: Path, backup: Path | None) -> None:
    """Put a moved executable back after its replacement failed."""
    if backup is None:
        return
    try:
        backup.replace(target)
    except OSError:
        log_event(_LOGGER, "kani.cleanup_failed", reason="rollback_error")


def _roll_back(published: list[tuple[Path, Path | None]]) -> None:
    """Restore every executable published before a later replacement failed."""
    for target, backup in reversed(published):
        if backup is None:
            # This target did not exist before the extract, so remove the new file.
            try:
                target.unlink(missing_ok=True)
            except OSError:
                log_event(_LOGGER, "kani.cleanup_failed", reason="rollback_error")
            continue
        _restore_backup(target, backup)


def _discard_backup(backup: Path) -> None:
    """Remove a backup after both executables were published.

    Unlike a rollback failure, this cleanup runs after publication succeeded,
    so a backup left behind is a standalone failure and fails the install
    rather than being reported and suppressed.
    """
    try:
        backup.unlink(missing_ok=True)
    except OSError as exc:
        log_event(_LOGGER, "kani.cleanup_failed", reason="backup_error")
        msg = f"failed to remove Kani backup '{backup}': {exc}"
        raise ProverToolError(msg) from exc


def _regular_member(archive: tarfile.TarFile, name: str) -> tarfile.TarInfo:
    """Return one regular archive member or raise a user-facing error."""
    try:
        member = archive.getmember(name)
    except KeyError as exc:
        msg = f"prebuilt Kani archive is missing {name}"
        raise ProverToolError(msg) from exc
    if not member.isfile():
        msg = f"prebuilt Kani archive entry {name} is not a regular file"
        raise ProverToolError(msg)
    if member.size > KANI_FRONTEND_MEMBER_MAX_BYTES:
        msg = (
            f"prebuilt Kani archive entry {name} is {member.size} bytes, "
            f"above the {KANI_FRONTEND_MEMBER_MAX_BYTES}-byte extraction limit"
        )
        raise ProverToolError(msg)
    return member


def _copy_member(
    archive: tarfile.TarFile,
    member: tarfile.TarInfo,
    target: Path,
) -> int:
    """Copy one archive member to `target` and return the bytes written."""
    source = archive.extractfile(member)
    if source is None:
        msg = f"prebuilt Kani archive entry {member.name} has no data"
        raise ProverToolError(msg)
    written = 0
    try:
        with source, target.open("wb") as output:
            while chunk := source.read(COPY_CHUNK_SIZE):
                written += len(chunk)
                if written > KANI_FRONTEND_MEMBER_MAX_BYTES:
                    msg = (
                        f"prebuilt Kani archive entry {member.name} exceeded "
                        f"the {KANI_FRONTEND_MEMBER_MAX_BYTES}-byte limit"
                    )
                    raise ProverToolError(msg)
                output.write(chunk)
        target.chmod(0o755)
    except OSError as exc:
        msg = f"failed to extract {member.name} to '{target}': {exc}"
        raise ProverToolError(msg) from exc
    return written
