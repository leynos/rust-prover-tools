"""Argument vectors and asset names derived from the pinned recipe.

Every producer step is an argument vector, never a shell string: the only
values interpolated here are a validated semantic version, the one supported
target triple and filesystem paths the workflow chose. Asset names are derived
from the same inputs so a release upload cannot be renamed by a hand edit.
"""

from __future__ import annotations

import re
import typing as typ

from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_snapshot.policy import is_safe_asset_name
from rust_prover_tools.kani_snapshot.validation import validate_target

if typ.TYPE_CHECKING:
    from pathlib import Path

__all__ = (
    "bundle_asset_name",
    "bundle_command",
    "launcher_asset_name",
    "launcher_extract_command",
    "launcher_install_command",
    "launcher_package_command",
    "setup_command",
    "toolchain_install_command",
)

_VERSION: typ.Final[re.Pattern[str]] = re.compile(r"\d+\.\d+\.\d+")
_PACKAGE: typ.Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9_-]+")


def _require_version(version: str) -> str:
    """Require a plain semantic version selector."""
    if _VERSION.fullmatch(version) is None:
        msg = f"Kani bundle version {version!r} must be a plain semver such as 0.68.0"
        raise ProverToolError(msg)
    return version


def _require_package(package: str) -> str:
    """Require a Cargo package name."""
    if _PACKAGE.fullmatch(package) is None:
        msg = f"Cargo package {package!r} must be a plain package name"
        raise ProverToolError(msg)
    return package


def _require_asset_name(name: str) -> str:
    """Require a derived name to be a plain basename."""
    if not is_safe_asset_name(name):
        msg = f"derived snapshot asset name {name!r} must be a plain basename"
        raise ProverToolError(msg)
    return name


def bundle_command(version: str) -> tuple[str, ...]:
    """Return the upstream bundle build command.

    Parameters
    ----------
    version : str
        Version selector passed after `--`, so Cargo forwards it to the
        `bundle` alias rather than interpreting it.

    Returns
    -------
    tuple[str, ...]
        Argument vector for `cargo bundle -- <version>`.

    Raises
    ------
    ProverToolError
        Raised when `version` is not a plain semantic version.
    """
    return ("cargo", "bundle", "--", _require_version(version))


def launcher_package_command(package: str) -> tuple[str, ...]:
    """Return the command that packages the launcher crate.

    Parameters
    ----------
    package : str
        Cargo package providing the launchers.

    Returns
    -------
    tuple[str, ...]
        Argument vector for `cargo package --package <package>`, which is how
        upstream produces the launcher crate a release installs from.

    Raises
    ------
    ProverToolError
        Raised when `package` is not a plain Cargo package name.
    """
    return ("cargo", "package", "--package", _require_package(package))


def launcher_extract_command(crate: Path) -> tuple[str, ...]:
    """Return the command that unpacks a packaged launcher crate.

    `cargo package` writes `<package>-<version>.crate` beside the unpacked
    `<package>-<version>/` directory it verifies. The design installs from
    that directory, so the archive is extracted beside the crate file rather
    than into a path the plan would have to invent.

    Parameters
    ----------
    crate : Path
        Packaged crate file Cargo produced.

    Returns
    -------
    tuple[str, ...]
        Argument vector extracting the archive into the crate's directory.
    """
    return ("tar", "-xzf", str(crate), "-C", str(crate.parent))


def launcher_install_command(package_dir: Path, root: Path) -> tuple[str, ...]:
    """Return the command that installs the packaged launchers.

    Parameters
    ----------
    package_dir : Path
        Unpacked launcher crate directory containing its `Cargo.toml`.
    root : Path
        Installation prefix; the launchers land in its `bin` directory.

    Returns
    -------
    tuple[str, ...]
        Argument vector for a locked install into a private prefix.
    """
    return (
        "cargo",
        "install",
        "--locked",
        "--path",
        str(package_dir),
        "--root",
        str(root),
    )


def toolchain_install_command(channel: str) -> tuple[str, ...]:
    """Return the command that installs the pinned toolchain.

    Parameters
    ----------
    channel : str
        Dated nightly channel from the recipe.

    Returns
    -------
    tuple[str, ...]
        Argument vector installing the channel with the components Kani needs.
    """
    return (
        "rustup",
        "toolchain",
        "install",
        channel,
        "--profile",
        "minimal",
        "--component",
        "llvm-tools,rustc-dev,rust-src,rustfmt",
    )


def setup_command(bundle: Path, toolchain: Path) -> tuple[str, ...]:
    """Return the local-bundle setup command.

    Parameters
    ----------
    bundle : Path
        Release bundle produced by `bundle_command`.
    toolchain : Path
        Prefix of the toolchain the bundle was built with.

    Returns
    -------
    tuple[str, ...]
        Argument vector for an offline `cargo kani setup`.
    """
    return (
        "cargo",
        "kani",
        "setup",
        "--use-local-bundle",
        str(bundle),
        "--use-local-toolchain",
        str(toolchain),
    )


def bundle_asset_name(version: str, target: str) -> str:
    """Return the upstream bundle asset name.

    Parameters
    ----------
    version : str
        Kani version.
    target : str
        The single supported target triple.

    Returns
    -------
    str
        `kani-<version>-<target>.tar.gz`, exactly as upstream names it.

    Raises
    ------
    ProverToolError
        Raised for a malformed version, an unsupported target or a name that
        is not a plain basename.
    """
    name = f"kani-{_require_version(version)}-{validate_target(target)}.tar.gz"
    return _require_asset_name(name)


def launcher_asset_name(version: str, target: str) -> str:
    """Return the launcher archive asset name this project publishes.

    Parameters
    ----------
    version : str
        Kani version.
    target : str
        The single supported target triple.

    Returns
    -------
    str
        `kani-<version>-launcher-<target>.tar.gz`.

    Raises
    ------
    ProverToolError
        Raised for a malformed version, an unsupported target or a name that
        is not a plain basename.
    """
    name = f"kani-{_require_version(version)}-launcher-{validate_target(target)}.tar.gz"
    return _require_asset_name(name)
