"""Verus release installation helpers.

This module owns the network and archive steps for installing a pinned Verus
release. It turns repository pins into a `VerusArchive`, downloads that archive
with the shared bounded HTTPS downloader, verifies its pinned SHA-256 digest,
unzips it, and normalizes the extracted directory to the `verus` layout
expected by `prover-tools verus run`.

Import `install_verus` when a caller needs the full installation workflow, and
import the smaller helpers when testing or validating individual release
steps. All external commands go through `rust_prover_tools.commands`, so tests
can replace them with cmd-mox shims.

Example
-------
```python
from pathlib import Path

from rust_prover_tools.verus.install import install_verus
from rust_prover_tools.verus.models import VerusInstallOptions, VerusPaths

messages = install_verus(
    VerusInstallOptions(paths=VerusPaths(repo_root=Path.cwd())),
)
```
"""

from __future__ import annotations

import contextlib
import logging
import re
import shutil
import stat
import tempfile
import typing as typ
from pathlib import Path
from time import monotonic

from rust_prover_tools.commands import CommandSpec, run_checked
from rust_prover_tools.downloads import PinnedAsset, download_pinned, sha256_file
from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.observability import log_event
from rust_prover_tools.versions import expected_sha256
from rust_prover_tools.verus.models import (
    VerusArchive,
    VerusInstallOptions,
    default_install_dir,
    read_verus_version,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

# Verus release archives are large, but a ceiling stops a malicious or broken
# response from filling the runner's temporary filesystem. The value follows
# the design document's 2 GiB compressed-payload bound.
VERUS_ARCHIVE_MAX_BYTES = 2 * 1024 * 1024 * 1024

# The release name is derived from repository input, so it is never used as a
# local path. A fixed name keeps the download inside the temporary directory
# even if a pin contains separators or traversal sequences.
ARCHIVE_FILE_NAME = "verus-release.zip"

# Version and target values become URL path segments and extracted-directory
# names. Restricting them to a single safe component also prevents a leading
# dash from being mistaken for an option by a later command.
RELEASE_COMPONENT_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

_LOGGER = logging.getLogger(__name__)
_EXECUTE_BITS: typ.Final[int] = stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH

# An installer phase can fail with a filesystem error or one of the library's
# own user-facing errors. Both are recorded under the phase's bounded reason.
_PHASE_FAILURES: typ.Final[tuple[type[Exception], ...]] = (
    OSError,
    ProverToolError,
)


def install_verus(options: VerusInstallOptions) -> tuple[str, ...]:
    """Install Verus and return user-facing output lines.

    Parameters
    ----------
    options : VerusInstallOptions
        Installation options including repository paths, release target,
        optional install directory, and release base URL.

    Returns
    -------
    tuple[str, ...]
        Status lines describing whether Verus was already installed or where a
        fresh installation was placed.

    Raises
    ------
    ProverToolError
        Raised when version pins, checksum pins, archive hashes, network
        transfers, or extracted directories are invalid.
    CommandFailedError
        Raised when `unzip` exits non-zero.
    OSError
        Raised when filesystem creation, removal, or moves fail.

    Notes
    -----
    The final delete-and-move sequence is not atomic. Concurrent installs that
    target the same `final_dir` can race, and one process may remove another
    process's work. Callers must avoid sharing an install directory between
    concurrent processes, or serialize access externally with a lock.
    """
    version = read_verus_version(options.paths)
    # Validate before the version reaches the default install path.
    _require_safe_release_component(version, label="version")
    _require_safe_release_component(options.target, label="target")
    install_dir = options.install_dir or default_install_dir(options.paths, version)
    installed_bin = install_dir / "verus" / "verus"
    started = monotonic()
    log_event(_LOGGER, "verus.install.start", version=version, target=options.target)
    with _phase("inspect"):
        installed = _has_installed_binary(installed_bin)
    if installed:
        log_event(_LOGGER, "verus.install.reuse", version=version)
        return (f"Verus {version} already installed at {install_dir / 'verus'}",)

    with _phase("archive"):
        archive = verus_archive(options, version)
    with _phase("prepare"):
        install_dir.mkdir(parents=True, exist_ok=True)
    with _phase("temporary"):
        temporary_directory = tempfile.TemporaryDirectory()
    with temporary_directory as directory:
        with _phase("download"):
            archive_path = download_archive(archive, Path(directory))
        with _phase("extract"):
            _unzip(archive_path, install_dir, archive.name)
        with _phase("locate"):
            extracted_dir = locate_extracted_verus_dir(install_dir, options.target)

    final_dir = install_dir / "verus"
    # Remove stale partial installs so the final tree is deterministic. This is
    # intentionally non-atomic and assumes the install directory is not shared.
    with _phase("publish"):
        if final_dir.exists():
            shutil.rmtree(final_dir)
        shutil.move(extracted_dir, final_dir)
    log_event(
        _LOGGER,
        "verus.install.complete",
        version=version,
        elapsed_seconds=monotonic() - started,
    )
    return (
        f"Installed Verus {version} in {final_dir}",
        f"Export VERUS_BIN={final_dir / 'verus'}",
    )


def _has_installed_binary(binary: Path) -> bool:
    """Return whether `binary` is an executable regular file."""
    # A missing path is an ordinary cache miss. Any other inspection failure is
    # reported instead, because treating an unreadable installation as absent
    # would reinstall over a broken environment.
    try:
        status = binary.stat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        msg = f"failed to inspect Verus installation at '{binary}': {exc}"
        raise ProverToolError(msg) from exc
    return stat.S_ISREG(status.st_mode) and bool(status.st_mode & _EXECUTE_BITS)


@contextlib.contextmanager
def _phase(reason: str) -> cabc.Generator[None]:
    """Record one bounded installer failure category for the enclosed step."""
    try:
        yield
    except _PHASE_FAILURES:
        log_event(_LOGGER, "verus.install.failed", reason=reason)
        raise


def _unzip(archive_path: Path, install_dir: Path, asset_name: str) -> None:
    """Extract a verified archive with `unzip`, recording the boundary."""
    log_event(_LOGGER, "verus.extract.start", asset=asset_name)
    run_checked(
        CommandSpec(("unzip", "-q", str(archive_path), "-d", str(install_dir))),
    )
    log_event(_LOGGER, "verus.extract.complete", asset=asset_name)


def verus_archive(options: VerusInstallOptions, version: str) -> VerusArchive:
    """Build the Verus release archive request.

    Parameters
    ----------
    options : VerusInstallOptions
        Repository pins supplying the target, download base URL, and checksum
        file.
    version : str
        Verus release version to request.

    Returns
    -------
    VerusArchive
        The archive name, download URL, and pinned SHA-256 digest.

    Raises
    ------
    ProverToolError
        Raised when the version or target is not a single safe path component,
        or when the checksum file has no row for the archive.
    """
    _require_safe_release_component(version, label="version")
    _require_safe_release_component(options.target, label="target")
    name = f"verus-{version}-{options.target}.zip"
    return VerusArchive(
        name=name,
        url=f"{options.base_url}/{version}/{name}",
        expected_sha=expected_sha256(options.paths.resolved_checksum_file(), name),
    )


def download_archive(archive: VerusArchive, temporary_directory: Path) -> Path:
    """Download a Verus archive into a temporary directory.

    The archive is written under a fixed local name. The release name comes
    from repository pins, so it must never select a path outside
    `temporary_directory`.

    Parameters
    ----------
    archive : VerusArchive
        Archive request built by `verus_archive`.
    temporary_directory : Path
        Directory that receives the downloaded archive.

    Returns
    -------
    Path
        The verified archive file inside `temporary_directory`.

    Raises
    ------
    ProverToolError
        Raised when the URL, transfer, byte ceiling, or digest check fails.
    """
    archive_path = temporary_directory / ARCHIVE_FILE_NAME
    asset = PinnedAsset(
        name=archive.name,
        url=archive.url,
        sha256=archive.expected_sha.lower(),
        max_bytes=VERUS_ARCHIVE_MAX_BYTES,
    )
    return download_pinned(asset, archive_path)


def verify_archive_sha(archive: VerusArchive, archive_path: Path) -> None:
    """Verify a downloaded Verus archive against its pinned checksum.

    `download_archive` already verifies the digest before it returns, so the
    installer does not need a second pass. This helper remains available for
    callers that verify an archive obtained elsewhere.

    Raises
    ------
    ProverToolError
        Raised when the actual SHA-256 digest does not match the expected
        digest.
    """
    actual_sha = calculate_sha256(archive_path)
    if actual_sha != archive.expected_sha.lower():
        msg = (
            f"SHA-256 mismatch for {archive.name}.\n"
            f"Expected: {archive.expected_sha}\n"
            f"Actual:   {actual_sha}"
        )
        raise ProverToolError(msg)


def calculate_sha256(path: Path) -> str:
    """Return the lowercase SHA-256 digest of a file.

    Parameters
    ----------
    path : Path
        File whose digest is calculated.

    Returns
    -------
    str
        Lowercase hexadecimal SHA-256 digest.

    Raises
    ------
    ProverToolError
        Raised when the file cannot be read.
    """
    return sha256_file(path)


def _require_safe_release_component(value: str, *, label: str) -> None:
    """Reject a Verus version or target that is unsafe in paths or URLs.

    Both values are repository-controlled and flow into a URL segment, an
    archive name, and an extracted-directory lookup. A leading dash or a path
    separator must fail before any of those uses.
    """
    if RELEASE_COMPONENT_PATTERN.fullmatch(value) is None:
        msg = (
            f"invalid Verus {label} {value!r}: expected ASCII letters, digits, "
            "'.', '_' or '-', starting with a letter or digit"
        )
        raise ProverToolError(msg)


def locate_extracted_verus_dir(install_dir: Path, target: str) -> Path:
    """Find the Verus directory extracted from a release archive.

    Parameters
    ----------
    install_dir : Path
        Directory searched for extracted Verus release directories.
    target : str
        Expected Verus release target suffix, such as `x86-linux`.

    Returns
    -------
    Path
        Located extracted Verus directory.

    Raises
    ------
    ProverToolError
        Raised when no matching directory is found or multiple fallback
        matches are ambiguous.
    """
    exact = install_dir / f"verus-{target}"
    if exact.is_dir():
        return exact
    matches = sorted(path for path in install_dir.glob("verus-*") if path.is_dir())
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        formatted = ", ".join(str(path) for path in matches)
        msg = f"ambiguous extracted Verus directories under {install_dir}: {formatted}"
        raise ProverToolError(msg)
    msg = f"unable to locate extracted Verus directory under {install_dir}"
    raise ProverToolError(msg)
