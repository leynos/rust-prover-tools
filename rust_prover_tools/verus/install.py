"""Verus release installation helpers.

This module owns the network and archive steps for installing a pinned Verus
release. It turns repository pins into a `VerusArchive`, downloads that archive
with Cuprum-backed `curl`, verifies its SHA-256 checksum with the platform
checksum tool, unzips it, and normalises the extracted directory to the
`verus` layout expected by `prover-tools verus run`.

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

import shutil
import tempfile
from pathlib import Path

from rust_prover_tools.commands import CommandSpec, run_checked
from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.versions import expected_sha256
from rust_prover_tools.verus.models import (
    VerusArchive,
    VerusInstallOptions,
    default_install_dir,
    is_executable_file,
    read_verus_version,
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
        Raised when version pins, checksum pins, archive hashes, or extracted
        directories are invalid.
    CommandFailedError
        Raised when `curl`, the checksum tool, or `unzip` exits non-zero.
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
    install_dir = options.install_dir or default_install_dir(options.paths, version)
    installed_bin = install_dir / "verus" / "verus"
    if is_executable_file(installed_bin):
        return (f"Verus {version} already installed at {install_dir / 'verus'}",)

    archive = verus_archive(options, version)
    install_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as temporary_directory:
        archive_path = download_archive(archive, Path(temporary_directory))
        verify_archive_sha(archive, archive_path)
        run_checked(
            CommandSpec(("unzip", "-q", str(archive_path), "-d", str(install_dir))),
        )

    extracted_dir = locate_extracted_verus_dir(install_dir, options.target)
    final_dir = install_dir / "verus"
    # Remove stale partial installs so the final tree is deterministic. This is
    # intentionally non-atomic and assumes the install directory is not shared.
    if final_dir.exists():
        shutil.rmtree(final_dir)
    shutil.move(extracted_dir, final_dir)
    return (
        f"Installed Verus {version} in {final_dir}",
        f"Export VERUS_BIN={final_dir / 'verus'}",
    )


def verus_archive(options: VerusInstallOptions, version: str) -> VerusArchive:
    """Build the Verus release archive request."""
    name = f"verus-{version}-{options.target}.zip"
    return VerusArchive(
        name=name,
        url=f"{options.base_url}/{version}/{name}",
        expected_sha=expected_sha256(options.paths.resolved_checksum_file(), name),
    )


def download_archive(archive: VerusArchive, temporary_directory: Path) -> Path:
    """Download a Verus archive into a temporary directory."""
    archive_path = temporary_directory / archive.name
    run_checked(
        CommandSpec((
            "curl",
            "-sSfL",
            "--connect-timeout",
            "15",
            "--max-time",
            "300",
            archive.url,
            "-o",
            str(archive_path),
        )),
    )
    return archive_path


def verify_archive_sha(archive: VerusArchive, archive_path: Path) -> None:
    """Verify a downloaded Verus archive against its pinned checksum.

    Parameters
    ----------
    archive : VerusArchive
        Archive metadata containing the expected SHA-256 digest.
    archive_path : Path
        Path to the downloaded archive file.

    Returns
    -------
    None
        Returns after `calculate_sha256` matches `archive.expected_sha`.

    Raises
    ------
    ProverToolError
        Raised when the actual SHA-256 digest does not match the expected
        digest.
    """
    actual_sha = calculate_sha256(archive_path)
    if actual_sha != archive.expected_sha:
        msg = (
            f"SHA-256 mismatch for {archive.name}.\n"
            f"Expected: {archive.expected_sha}\n"
            f"Actual:   {actual_sha}"
        )
        raise ProverToolError(msg)


def calculate_sha256(path: Path) -> str:
    """Calculate a SHA-256 checksum using the platform checksum tool.

    Parameters
    ----------
    path : Path
        File path to checksum.

    Returns
    -------
    str
        SHA-256 hexadecimal digest string.

    Raises
    ------
    ProverToolError
        Raised when neither `sha256sum` nor `shasum` is available, or when the
        checksum command produces empty output.
    """
    if shutil.which("sha256sum") is not None:
        result = run_checked(CommandSpec(("sha256sum", str(path))))
    elif shutil.which("shasum") is not None:
        result = run_checked(CommandSpec(("shasum", "-a", "256", str(path))))
    else:
        msg = "missing SHA-256 tool (sha256sum or shasum)."
        raise ProverToolError(msg)

    stdout = result.stdout.strip()
    if not stdout:
        detail = result.stderr.strip() or result.stdout
        msg = f"empty stdout from sha256sum/shasum: {detail}"
        raise ProverToolError(msg)
    return stdout.split()[0]


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
