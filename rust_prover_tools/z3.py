"""Pinned Z3 solver installation for Verus.

Downloads the pinned official Z3 zip through the bounded verifier in
`rust_prover_tools.downloads`, extracts only `<release>/bin/z3`, and records
the installed version with the extracted binary's SHA-256. Reuse rehashes the
binary and matches that digest.
"""

from __future__ import annotations

import contextlib
import dataclasses as dc
import hashlib
import json
import logging
import stat
import typing as typ
import zipfile
from time import monotonic

from rust_prover_tools.binary_assets import (
    COPY_CHUNK_SIZE,
    Z3_ASSET,
    Z3_EXTRACTED_MAX_BYTES,
    Z3_MEMBER,
    Z3_VERSION,
)
from rust_prover_tools.downloads import download_pinned, sha256_file
from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.observability import log_event
from rust_prover_tools.path_resolution import resolve_configured_path
from rust_prover_tools.platforms import require_linux_x86_64

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import os
    from pathlib import Path

__all__ = (
    "Z3InstallOptions",
    "default_z3_cache_dir",
    "default_z3_install_dir",
    "install_z3",
)

MARKER_NAME = ".z3-version"
_EXECUTE_BITS: typ.Final[int] = stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
_MARKER_KEYS: typ.Final[frozenset[str]] = frozenset({"version", "sha256"})
# Ruff emits PEP 758 here for the py314 target, but the pylint gate runs on
# Python 3.12, which cannot parse that form, so the tuple is named.
_MARKER_PARSE_ERRORS: typ.Final = (ValueError, RecursionError)
_SHA256_HEX_CHARS: typ.Final[frozenset[str]] = frozenset("0123456789abcdef")
_SHA256_HEX_LENGTH: typ.Final[int] = 64

_LOGGER = logging.getLogger(__name__)


class _Extraction(typ.NamedTuple):
    """Size and digest of one extracted archive member.

    Attributes
    ----------
    size : int
        Bytes written to the installed binary.
    sha256 : str
        Lowercase hexadecimal SHA-256 digest of those bytes.
    """

    size: int
    sha256: str


@dc.dataclass(frozen=True, slots=True)
class Z3InstallOptions:
    """Options for installing the pinned Z3 solver.

    Attributes
    ----------
    repo_root : Path
        Repository root used to resolve relative cache and install paths.
    install_dir : Path | None
        Directory for the `z3` binary. Defaults to `.cache/z3/<version>`
        below the repository root.
    cache_dir : Path | None
        Directory for verified release archives. Defaults to `.cache/z3`
        below the repository root.
    """

    repo_root: Path
    install_dir: Path | None = None
    cache_dir: Path | None = None


def default_z3_install_dir(repo_root: Path) -> Path:
    """Return the versioned install directory below the repository cache."""
    return default_z3_cache_dir(repo_root) / Z3_VERSION


def default_z3_cache_dir(repo_root: Path) -> Path:
    """Return the `.cache/z3` archive cache below the repository root."""
    return repo_root / ".cache" / "z3"


def install_z3(options: Z3InstallOptions) -> tuple[str, ...]:
    """Install the pinned Z3 solver and return user-facing output lines.

    Parameters
    ----------
    options : Z3InstallOptions
        Repository root and optional install and cache directories.

    Returns
    -------
    tuple[str, ...]
        Status line and the `VERUS_Z3_PATH` export instruction.

    Raises
    ------
    ProverToolError
        Raised for an unsupported host, a download limit, an invalid archive,
        a missing member, or a filesystem failure.
    """
    require_linux_x86_64("Z3")
    install_dir = resolve_configured_path(
        options.install_dir,
        options.repo_root,
        default_z3_install_dir(options.repo_root),
    )
    cache_dir = resolve_configured_path(
        options.cache_dir,
        options.repo_root,
        default_z3_cache_dir(options.repo_root),
    )
    binary = install_dir / "z3"
    marker = install_dir / MARKER_NAME
    started = monotonic()
    log_event(_LOGGER, "z3.install.start", version=Z3_VERSION)
    try:
        blocker = _reuse_blocker(binary, marker)
    except ProverToolError:
        _log_failure("inspect")
        raise
    if blocker is None:
        log_event(_LOGGER, "z3.install.reuse", version=Z3_VERSION)
        return (
            f"Z3 {Z3_VERSION} already installed at {binary}",
            f"Export VERUS_Z3_PATH={binary}",
        )
    log_event(
        _LOGGER,
        "z3.install.cache_miss",
        version=Z3_VERSION,
        reason=blocker,
    )

    try:
        archive = download_pinned(Z3_ASSET, cache_dir / Z3_ASSET.name)
    except ProverToolError:
        _log_failure("download")
        raise
    try:
        extracted = _extract_z3(archive, install_dir, binary)
    except ProverToolError:
        _log_failure("extract")
        raise
    try:
        _write_marker(marker, extracted.sha256)
    except ProverToolError:
        _log_failure("marker")
        raise
    log_event(
        _LOGGER,
        "z3.install.complete",
        version=Z3_VERSION,
        bytes=extracted.size,
        elapsed_seconds=monotonic() - started,
    )
    return (
        f"Installed Z3 {Z3_VERSION} at {binary}",
        f"Export VERUS_Z3_PATH={binary}",
    )


def _log_failure(reason: str) -> None:
    """Record one bounded installer failure category."""
    log_event(_LOGGER, "z3.install.failed", reason=reason)


def _reuse_blocker(binary: Path, marker: Path) -> str | None:
    """Return the bounded reason reuse is refused, or `None` to reuse."""
    # Only a missing path means "absent"; other OS errors are reported.
    binary_status = _optional_stat(binary)
    if binary_status is None or not stat.S_ISREG(binary_status.st_mode):
        return "binary_missing"
    if not _has_execute_bit(binary_status):
        return "not_executable"
    marker_text = _optional_text(marker)
    if marker_text is None:
        return "marker_missing"
    recorded_digest = _recorded_digest(marker_text)
    if recorded_digest is None:
        return "marker_unusable"
    # A read failure here is reported by the caller as an inspect failure.
    if sha256_file(binary) != recorded_digest:
        return "digest_mismatch"
    return None


def _recorded_digest(text: str) -> str | None:
    """Return the digest recorded by a well-formed marker, or `None`.

    A marker is usable only when it is a JSON object with exactly the
    `version` and `sha256` keys, both strings, the current pinned version,
    and a lowercase SHA-256 digest. Anything else is a cache miss that a
    fresh installation repairs.
    """
    try:
        parsed: object = json.loads(text)
    except _MARKER_PARSE_ERRORS:
        # Every failure here means the marker is unusable, never that the
        # run should fail.
        return None
    if not isinstance(parsed, dict) or set(parsed) != _MARKER_KEYS:
        return None
    version = parsed["version"]
    digest = parsed["sha256"]
    if not isinstance(version, str) or not isinstance(digest, str):
        return None
    if version != Z3_VERSION:
        return None
    if len(digest) != _SHA256_HEX_LENGTH or not set(digest) <= _SHA256_HEX_CHARS:
        return None
    return digest


def _has_execute_bit(status: os.stat_result) -> bool:
    """Return whether the recorded mode carries any execute bit."""
    return bool(status.st_mode & _EXECUTE_BITS)


def _optional_stat(path: Path) -> os.stat_result | None:
    """Return the status of `path`, or `None` when it does not exist."""
    try:
        return path.stat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        msg = f"failed to inspect Z3 installation at '{path}': {exc}"
        raise ProverToolError(msg) from exc


def _optional_text(path: Path) -> str | None:
    """Return the text of `path`, or `None` when it is absent or not UTF-8."""
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except UnicodeDecodeError:
        # A marker that is not UTF-8 cannot describe an installation this
        # release produced, so it is a cache miss, not a hard failure.
        return None
    except OSError as exc:
        msg = f"failed to read Z3 installation state '{path}': {exc}"
        raise ProverToolError(msg) from exc


@contextlib.contextmanager
def _temporary_file(path: Path) -> cabc.Generator[None]:
    """Remove a temporary file without replacing an earlier failure."""
    # A cleanup failure after a successful block is raised so the installer
    # records a bounded failure. If the block already failed, the cleanup
    # failure is recorded and suppressed because replacing the primary error
    # would hide the real cause.
    try:
        yield
    except BaseException:
        _discard_temporary(path, has_prior_failure=True)
        raise
    _discard_temporary(path, has_prior_failure=False)


def _discard_temporary(path: Path, *, has_prior_failure: bool) -> None:
    """Remove a temporary file and report a cleanup failure at the right level."""
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        if has_prior_failure:
            log_event(_LOGGER, "z3.cleanup_failed", reason="cleanup_error")
            return
        msg = f"failed to remove temporary Z3 file '{path}': {exc}"
        raise ProverToolError(msg) from exc


def _extract_z3(
    archive_path: Path,
    install_dir: Path,
    binary: Path,
) -> _Extraction:
    """Extract the pinned `z3` member and return its size and digest."""
    temporary = install_dir / ".z3.download"
    with _temporary_file(temporary):
        extracted = _write_z3_member(archive_path, install_dir, temporary, binary)
    log_event(
        _LOGGER,
        "z3.extract.complete",
        asset=Z3_ASSET.name,
        bytes=extracted.size,
    )
    return extracted


def _write_z3_member(
    archive_path: Path,
    install_dir: Path,
    temporary: Path,
    binary: Path,
) -> _Extraction:
    """Write the bounded member to `temporary` and install it as `binary`."""
    digest = hashlib.sha256()
    written = 0
    try:
        install_dir.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(archive_path) as archive:
            member = _z3_member(archive)
            with archive.open(member) as source, temporary.open("wb") as output:
                while chunk := source.read(COPY_CHUNK_SIZE):
                    written += len(chunk)
                    if written > Z3_EXTRACTED_MAX_BYTES:
                        msg = (
                            f"Z3 archive member {Z3_MEMBER} exceeded the "
                            f"{Z3_EXTRACTED_MAX_BYTES}-byte extraction limit"
                        )
                        raise ProverToolError(msg)
                    digest.update(chunk)
                    output.write(chunk)
        temporary.chmod(0o755)
        temporary.replace(binary)
    except (OSError, zipfile.BadZipFile) as exc:
        msg = f"failed to extract Z3 archive '{archive_path}': {exc}"
        raise ProverToolError(msg) from exc
    return _Extraction(size=written, sha256=digest.hexdigest())


def _z3_member(archive: zipfile.ZipFile) -> zipfile.ZipInfo:
    """Return the pinned Z3 executable member."""
    try:
        member = archive.getinfo(Z3_MEMBER)
    except KeyError as exc:
        msg = f"Z3 archive is missing {Z3_MEMBER}"
        raise ProverToolError(msg) from exc
    if member.is_dir():
        msg = f"Z3 archive entry {Z3_MEMBER} is a directory"
        raise ProverToolError(msg)
    if member.file_size > Z3_EXTRACTED_MAX_BYTES:
        msg = (
            f"Z3 archive entry {Z3_MEMBER} is {member.file_size} bytes, "
            f"above the {Z3_EXTRACTED_MAX_BYTES}-byte extraction limit"
        )
        raise ProverToolError(msg)
    return member


def _write_marker(marker: Path, digest: str) -> None:
    """Record the installed version and binary digest beside the binary."""
    temporary = marker.with_name(f".{marker.name}.download")
    # Sorted keys keep the marker byte-for-byte canonical, so an unchanged
    # installation never looks different to a later run.
    payload = (
        json.dumps(
            {"version": Z3_VERSION, "sha256": digest},
            sort_keys=True,
        )
        + "\n"
    )
    with _temporary_file(temporary):
        try:
            temporary.write_text(payload, encoding="utf-8")
            temporary.replace(marker)
        except OSError as exc:
            msg = f"failed to record Z3 installation in '{marker}': {exc}"
            raise ProverToolError(msg) from exc
    log_event(_LOGGER, "z3.marker.recorded", version=Z3_VERSION)
