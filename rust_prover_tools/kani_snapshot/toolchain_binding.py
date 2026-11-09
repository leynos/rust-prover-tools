"""Verification of extracted toolchain bytes before any compiler runs.

Setup installs a Rust toolchain and then a probe executes `rustc` and
`kani-compiler`. A version string is an assertion by those binaries about
themselves, not evidence about their bytes, so the reviewed manifest also
records the digest of the installed compiler, of the extracted libraries the
binary loads, and of the rustup component list. This module compares the
installed tree with those records before the probe runs.

Rustup component artifacts are recorded with their URLs, sizes and digests for
audit and reproducibility. They are not re-derived here: installation never
re-downloads a component, so there are no downloaded bytes to compare. The
extracted files that a probe actually executes or loads are what this module
verifies.
"""

from __future__ import annotations

import stat
import typing as typ

from rust_prover_tools.downloads import sha256_file
from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_snapshot.validation import is_contained_relative_path

if typ.TYPE_CHECKING:
    from pathlib import Path

    from rust_prover_tools.kani_snapshot.models import SnapshotManifest
    from rust_prover_tools.kani_snapshot.snapshot_layout import SnapshotRuntime

__all__ = ("toolchain_root", "verify_toolchain")

#: Installed compiler the probe executes.
_RUSTC: typ.Final[str] = "bin/rustc"

#: Rustup's record of the components installed in one toolchain.
_RUSTUP_COMPONENTS: typ.Final[str] = "lib/rustlib/components"


def toolchain_root(runtime: SnapshotRuntime) -> Path:
    """Return the generation-private toolchain root for one runtime."""
    return runtime.layout.rustup_home / "toolchains" / runtime.toolchain


def verify_toolchain(runtime: SnapshotRuntime, manifest: SnapshotManifest) -> None:
    """Require installed toolchain bytes to match the reviewed manifest.

    Parameters
    ----------
    runtime : SnapshotRuntime
        Runtime naming the toolchain that setup installed.
    manifest : SnapshotManifest
        Manifest whose toolchain records name the expected bytes.

    Raises
    ------
    ProverToolError
        Raised when the compiler, a recorded library or the rustup component
        list is missing, is not a regular file, or has a different digest.
        Every check runs before the probe, so a mismatch can never be followed
        by executing the bytes it rejected.
    """
    root = toolchain_root(runtime)
    toolchain = manifest.toolchain
    checks = (
        ("compiler", _RUSTC, toolchain.rustc_sha256),
        ("component records", _RUSTUP_COMPONENTS, toolchain.rustup_components_sha256),
        *(
            (f"library '{library.path}'", library.path, library.sha256)
            for library in toolchain.libraries
        ),
    )
    for description, relative, expected in checks:
        _require_matching(root, relative, expected, description)


def _require_matching(
    root: Path,
    relative: str,
    expected: str,
    description: str,
) -> None:
    """Require one contained toolchain file to match its recorded digest."""
    if not is_contained_relative_path(relative):
        msg = f"snapshot toolchain {description} is not a contained relative path"
        raise ProverToolError(msg)
    path = root / relative
    try:
        status = path.stat()
    except OSError as exc:
        msg = f"snapshot toolchain {description} is missing: {exc}"
        raise ProverToolError(msg) from exc
    if not stat.S_ISREG(status.st_mode):
        msg = f"snapshot toolchain {description} is not a regular file: {path}"
        raise ProverToolError(msg)
    actual = sha256_file(path)
    if actual != expected:
        msg = (
            f"snapshot toolchain {description} does not match the manifest: "
            f"expected {expected}, got {actual}"
        )
        raise ProverToolError(msg)
