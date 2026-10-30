"""Prebuilt Kani binary installation.

The installer downloads the cargo-quickinstall frontend and the matching Kani
native bundle, verifies both pinned SHA-256 digests, extracts only the
`cargo-kani` and `kani` executables, and then runs `cargo kani setup` with the
verified local bundle. Compiling Kani from source remains available through the
opt-in `--compile` fallback in `rust_prover_tools.kani`.
"""

from __future__ import annotations

import dataclasses as dc
import os
import tarfile
from pathlib import Path

from rust_prover_tools.binary_assets import (
    COPY_CHUNK_SIZE,
    KANI_BUNDLE_ASSET,
    KANI_FRONTEND_ASSET,
    KANI_FRONTEND_MEMBER_MAX_BYTES,
    KANI_FRONTEND_MEMBERS,
    PREBUILT_KANI_VERSION,
)
from rust_prover_tools.commands import CommandSpec, run_checked
from rust_prover_tools.downloads import download_pinned
from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.path_resolution import resolve_configured_path
from rust_prover_tools.platforms import require_linux_x86_64

__all__ = (
    "KaniBinaryInstallOptions",
    "default_kani_cache_dir",
    "default_kani_install_dir",
    "install_kani_binary",
)


@dc.dataclass(frozen=True, slots=True)
class KaniBinaryInstallOptions:
    """Options for installing prebuilt Kani binaries.

    Attributes
    ----------
    repo_root : Path
        Repository root used to resolve relative cache and install paths.
    version : str
        Requested Kani version. It must match the pinned prebuilt release.
    version_file : Path
        Version file named in status messages.
    is_override : bool
        Whether the version came from an explicit override rather than the
        version file.
    install_dir : Path | None
        Directory for `cargo-kani` and `kani`. Defaults to Cargo's binary
        directory.
    cache_dir : Path | None
        Directory for verified release archives. Defaults to
        `.cache/kani` below the repository root.
    should_setup : bool
        Whether to run `cargo kani setup --use-local-bundle`.
    should_verify : bool
        Whether to run `cargo kani --version`.
    """

    repo_root: Path
    version: str
    version_file: Path
    is_override: bool = False
    install_dir: Path | None = None
    cache_dir: Path | None = None
    should_setup: bool = True
    should_verify: bool = True


def default_kani_install_dir() -> Path:
    """Return Cargo's binary directory.

    Returns
    -------
    Path
        `$CARGO_HOME/bin`, or `$HOME/.cargo/bin` when `CARGO_HOME` is unset.
    """
    cargo_home = os.environ.get("CARGO_HOME")
    if cargo_home:
        return Path(cargo_home).expanduser() / "bin"
    return Path.home() / ".cargo" / "bin"


def default_kani_cache_dir(repo_root: Path) -> Path:
    """Return the default Kani archive cache directory.

    Parameters
    ----------
    repo_root : Path
        Repository root.

    Returns
    -------
    Path
        `.cache/kani` below the repository root.
    """
    return repo_root / ".cache" / "kani"


def install_kani_binary(options: KaniBinaryInstallOptions) -> tuple[str, ...]:
    """Install the pinned prebuilt Kani release.

    Parameters
    ----------
    options : KaniBinaryInstallOptions
        Version, paths, and optional setup and verification flags.

    Returns
    -------
    tuple[str, ...]
        User-facing status messages for completed steps.

    Raises
    ------
    ProverToolError
        Raised for an unsupported host, an unpinned version, an invalid
        archive, a download limit, or a filesystem failure.
    CommandFailedError
        Raised when `cargo kani setup` or `cargo kani --version` fails.
    """
    require_linux_x86_64(
        "Kani",
        fallback_hint="Re-run with --compile to build Kani from source.",
    )
    if options.version != PREBUILT_KANI_VERSION:
        msg = (
            f"no pinned prebuilt Kani binaries for {options.version}; "
            f"the bundled prebuilt release is {PREBUILT_KANI_VERSION}. "
            "Re-run with --compile to build Kani from source."
        )
        raise ProverToolError(msg)

    install_dir = resolve_configured_path(
        options.install_dir,
        options.repo_root,
        default_kani_install_dir(),
    )
    cache_dir = resolve_configured_path(
        options.cache_dir,
        options.repo_root,
        default_kani_cache_dir(options.repo_root),
    )
    source = f"from {options.version_file}" if not options.is_override else "(override)"
    lines = [f"Installing Kani {options.version} prebuilt binaries {source}."]
    frontend, bundle = _download_assets(cache_dir)
    lines.append(
        f"Extracting Kani {options.version} binaries into {install_dir}.",
    )
    _extract_frontend(frontend, install_dir)

    if options.should_setup:
        lines.append(f"Running cargo kani setup for Kani {options.version}.")
        _run_setup(install_dir, bundle)

    if options.should_verify:
        lines.append("Verifying cargo kani is callable.")
        verifier_output = _run_verify(install_dir)
        if verifier_output:
            lines.append(verifier_output)

    return tuple(lines)


def _download_assets(cache_dir: Path) -> tuple[Path, Path]:
    """Download both Kani archives into the cache."""
    frontend = download_pinned(
        KANI_FRONTEND_ASSET,
        cache_dir / KANI_FRONTEND_ASSET.name,
    )
    bundle = download_pinned(
        KANI_BUNDLE_ASSET,
        cache_dir / KANI_BUNDLE_ASSET.name,
    )
    return frontend, bundle


def _extract_frontend(archive_path: Path, install_dir: Path) -> None:
    """Extract the two Kani frontend executables atomically."""
    pending: list[tuple[Path, Path]] = []
    try:
        install_dir.mkdir(parents=True, exist_ok=True)
        with tarfile.open(archive_path, "r:gz") as archive:
            try:
                for name in KANI_FRONTEND_MEMBERS:
                    member = _regular_member(archive, name)
                    temporary = install_dir / f".{name}.download"
                    _copy_member(archive, member, temporary)
                    pending.append((temporary, install_dir / name))
                for temporary, target in pending:
                    temporary.replace(target)
            finally:
                for temporary, _ in pending:
                    temporary.unlink(missing_ok=True)
    except (OSError, tarfile.TarError) as exc:
        msg = f"failed to extract prebuilt Kani archive '{archive_path}': {exc}"
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
) -> None:
    """Copy one archive member to `target` with executable permissions."""
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


def _path_env(install_dir: Path) -> dict[str, str]:
    """Return a PATH overlay that makes the extracted Kani binaries visible."""
    entries = [str(install_dir)]
    inherited = os.environ.get("PATH")
    if inherited:
        entries.append(inherited)
    return {"PATH": os.pathsep.join(entries)}


def _run_setup(install_dir: Path, bundle: Path) -> None:
    """Run `cargo kani setup` with the verified local bundle."""
    run_checked(
        CommandSpec(
            (
                "cargo",
                "kani",
                "setup",
                "--use-local-bundle",
                str(bundle),
            ),
            env=_path_env(install_dir),
        ),
    )


def _run_verify(install_dir: Path) -> str:
    """Run `cargo kani --version` with the extracted binaries on PATH."""
    result = run_checked(
        CommandSpec(
            ("cargo", "kani", "--version"),
            env=_path_env(install_dir),
        ),
    )
    return result.stdout.strip()
