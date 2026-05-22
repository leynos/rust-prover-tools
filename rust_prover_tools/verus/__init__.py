"""Public Verus workflow API.

This package facade exposes Verus installation, proof-running, path models,
and result exceptions from the focused implementation modules. Clients usually
construct `VerusPaths` and `VerusRunOptions`, then call `run_verus`, which
returns `tuple[str, ...]` wrapper messages and streams verifier output through
Cuprum.
"""

from __future__ import annotations

from rust_prover_tools.verus.install import (
    calculate_sha256,
    install_verus,
    locate_extracted_verus_dir,
    verify_archive_sha,
)
from rust_prover_tools.verus.models import (
    DEFAULT_TARGET,
    EXAMPLE_VERUS_PROOF_PATH,
    VERUS_BASE_URL,
    VERUS_CHECKSUM_PATH,
    VERUS_VERSION_PATH,
    ProofFailureContext,
    VerusArchive,
    VerusInstallOptions,
    VerusPaths,
    VerusProofFailedError,
    VerusRunOptions,
    default_install_dir,
    is_executable_file,
    read_verus_version,
)
from rust_prover_tools.verus.run import (
    ensure_toolchain_installed,
    ensure_verus_toolchain,
    fallback_warning,
    resolve_default_verus,
    resolve_verus_bin,
    run_verus,
)

__all__ = [
    "DEFAULT_TARGET",
    "EXAMPLE_VERUS_PROOF_PATH",
    "VERUS_BASE_URL",
    "VERUS_CHECKSUM_PATH",
    "VERUS_VERSION_PATH",
    "ProofFailureContext",
    "VerusArchive",
    "VerusInstallOptions",
    "VerusPaths",
    "VerusProofFailedError",
    "VerusRunOptions",
    "calculate_sha256",
    "default_install_dir",
    "ensure_toolchain_installed",
    "ensure_verus_toolchain",
    "fallback_warning",
    "install_verus",
    "is_executable_file",
    "locate_extracted_verus_dir",
    "read_verus_version",
    "resolve_default_verus",
    "resolve_verus_bin",
    "run_verus",
    "verify_archive_sha",
]
