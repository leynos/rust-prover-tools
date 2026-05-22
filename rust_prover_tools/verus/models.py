"""Shared Verus workflow models and constants.

This module defines the dataclasses, constants, and small path helpers used by
the Verus installer and proof runner. Callers normally construct
`VerusInstallOptions` or `VerusRunOptions` with a `VerusPaths` value, then pass
those options to `rust_prover_tools.verus.install.install_verus` or
`rust_prover_tools.verus.run.run_verus`. The helpers here perform only local
path and file checks; they do not run external commands or mutate the
filesystem beyond reading configured pin files.

Example
-------
```python
from pathlib import Path

from rust_prover_tools.verus.models import VerusPaths, default_install_dir

paths = VerusPaths(repo_root=Path.cwd())
install_dir = default_install_dir(paths, "2025.01.01")
```
"""

from __future__ import annotations

import dataclasses as dc
import os
from pathlib import Path

from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.versions import read_required_file

VERUS_VERSION_PATH = Path("tools/verus/VERSION")
VERUS_CHECKSUM_PATH = Path("tools/verus/SHA256SUMS")
EXAMPLE_VERUS_PROOF_PATH = Path(
    os.environ.get(
        "RUST_PROVER_TOOLS_VERUS_PROOF_PATH",
        "verus/edge_harvest_proofs.rs",
    ),
)
"""Configurable example proof path used when no proof file is provided."""
VERUS_BASE_URL = "https://github.com/verus-lang/verus/releases/download/release"
DEFAULT_TARGET = "x86-linux"


@dc.dataclass(frozen=True, slots=True)
class VerusPaths:
    """Repository-relative Verus path configuration.

    Attributes
    ----------
    repo_root : Path
        Repository root used to resolve default Verus paths.
    version_file : Path | None
        Optional version pin override.
    checksum_file : Path | None
        Optional checksum file override.
    """

    repo_root: Path
    version_file: Path | None = None
    checksum_file: Path | None = None

    def resolved_version_file(self) -> Path:
        """Return the configured or default version file."""
        return self.version_file or self.repo_root / VERUS_VERSION_PATH

    def resolved_checksum_file(self) -> Path:
        """Return the configured or default checksum file."""
        return self.checksum_file or self.repo_root / VERUS_CHECKSUM_PATH


@dc.dataclass(frozen=True, slots=True)
class VerusInstallOptions:
    """Options for installing Verus.

    Attributes
    ----------
    paths : VerusPaths
        Verus path configuration.
    target : str
        Verus release target.
    install_dir : Path | None
        Optional installation directory override.
    base_url : str
        Verus release base URL.
    """

    paths: VerusPaths
    target: str = DEFAULT_TARGET
    install_dir: Path | None = None
    base_url: str = VERUS_BASE_URL


@dc.dataclass(frozen=True, slots=True)
class VerusRunOptions:
    """Options for running Verus proofs.

    Attributes
    ----------
    paths : VerusPaths
        Verus path configuration.
    proof_file : Path | None
        Optional proof file override.
    install_dir : Path | None
        Optional installation directory override.
    verus_bin : str | None
        Optional binary, directory, or command override.
    should_ensure_toolchain : bool
        Whether to install the required Rust toolchain.
    should_install_missing : bool
        Whether to install Verus if the default binary is missing.
    extra_args : tuple[str, ...]
        Extra Verus arguments appended after the proof file.
    """

    paths: VerusPaths
    proof_file: Path | None = None
    install_dir: Path | None = None
    verus_bin: str | None = None
    should_ensure_toolchain: bool = True
    should_install_missing: bool = True
    extra_args: tuple[str, ...] = ()


@dc.dataclass(frozen=True, slots=True)
class VerusArchive:
    """A Verus release archive request.

    Attributes
    ----------
    name : str
        Release archive file name.
    url : str
        Download URL for the release archive.
    expected_sha : str
        Expected SHA-256 digest read from the checksum pin file.
    """

    name: str
    url: str
    expected_sha: str


@dc.dataclass(frozen=True, slots=True)
class ProofFailureContext:
    """Context printed when Verus proof verification fails.

    Attributes
    ----------
    exit_code : int
        Process exit code returned by the verifier.
    binary : Path
        Path to the Verus binary used for the proof run.
    proof_file : Path
        Path to the proof file that failed verification.
    toolchain : str
        Rust toolchain identifier used for the proof run.
    """

    exit_code: int
    binary: Path
    proof_file: Path
    toolchain: str


class VerusProofFailedError(ProverToolError):
    """Raised when Verus proof verification exits non-zero.

    Attributes
    ----------
    exit_code : int
        Non-zero exit code returned by the Verus verifier.
    """

    def __init__(self, context: ProofFailureContext) -> None:
        """Initialise the proof failure.

        Parameters
        ----------
        context : ProofFailureContext
            Failure context containing the exit code, binary, proof file, and
            toolchain used to build the user-facing error message.
        """
        super().__init__(
            f"Verus proofs failed (exit {context.exit_code}).\n"
            f"Binary: {context.binary}\n"
            f"Proof file: {context.proof_file}\n"
            f"Toolchain: {context.toolchain}",
        )
        self.exit_code = context.exit_code


def read_verus_version(paths: VerusPaths) -> str:
    """Read the Verus version pin.

    Parameters
    ----------
    paths : VerusPaths
        Path configuration whose `resolved_version_file()` must point to a
        non-empty UTF-8 version pin.

    Returns
    -------
    str
        Pinned Verus version string.
    """
    return read_required_file(paths.resolved_version_file(), description="version file")


def default_install_dir(paths: VerusPaths, version: str) -> Path:
    """Return the default Verus installation directory.

    Parameters
    ----------
    paths : VerusPaths
        Repository path configuration.
    version : str
        Verus version used as the directory name.

    Returns
    -------
    Path
        Default installation directory under `.verus/<version>`.
    """
    return paths.repo_root / ".verus" / version


def is_executable_file(path: Path) -> bool:
    """Return whether `path` is an executable file.

    Parameters
    ----------
    path : Path
        Filesystem path to test.

    Returns
    -------
    bool
        `True` when `path` exists, is a file, and passes `os.X_OK`.
    """
    return path.is_file() and os.access(path, os.X_OK)
