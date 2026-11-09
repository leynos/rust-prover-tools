"""Options and injectable boundaries for snapshot installation.

The pipeline is deliberately split from the external tools it drives. Tests
supply a setup runner, a compiler probe and an attestation verifier, while these
defaults execute the packaged launcher and `gh`.
"""

from __future__ import annotations

import collections.abc as cabc
import dataclasses as dc
import typing as typ

from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_snapshot.install_defaults import (
    probe_generation,
    run_local_bundle_setup,
)
from rust_prover_tools.kani_snapshot.integrity import verify_attestation

if typ.TYPE_CHECKING:
    from pathlib import Path

    from rust_prover_tools.kani_snapshot.identity import SnapshotRuntime
    from rust_prover_tools.kani_snapshot.integrity import AttestationPolicy
    from rust_prover_tools.kani_snapshot.models import SnapshotPin
    from rust_prover_tools.kani_snapshot.receipt_model import SetupEvidence

__all__ = (
    "ATTESTATION_ASSET_NAME",
    "CHECKSUMS_ASSET_NAME",
    "MANIFEST_ASSET_NAME",
    "AttestationVerifier",
    "InstallBudget",
    "InstallHooks",
    "InstallOptions",
    "ProbeRunner",
    "SetupRunner",
)

#: The release manifest whose exact bytes the pin hashes.
MANIFEST_ASSET_NAME: typ.Final[str] = "manifest.json"

#: The release checksum list covering payloads and the manifest.
CHECKSUMS_ASSET_NAME: typ.Final[str] = "SHA256SUMS"

#: The Sigstore bundle verifying the manifest attestation.
ATTESTATION_ASSET_NAME: typ.Final[str] = "manifest.sigstore.jsonl"

#: Honest upper bounds for the small, non-manifest transport files.
MANIFEST_MAX_BYTES: typ.Final[int] = 4 * 1024 * 1024
ATTESTATION_MAX_BYTES: typ.Final[int] = 16 * 1024 * 1024
CHECKSUMS_MAX_BYTES: typ.Final[int] = 1024 * 1024

SetupRunner = cabc.Callable[["SnapshotRuntime", "Path", "float | None"], None]
ProbeRunner = cabc.Callable[["SnapshotRuntime", "float | None"], "SetupEvidence"]


class InstallBudget(typ.Protocol):
    """A monotonic installation budget shared by transport and hooks."""

    def check(self, phase: str) -> None:
        """Raise when the budget has expired during `phase`."""

    def remaining(self) -> float:
        """Return the seconds left, never negative."""


class AttestationVerifier(typ.Protocol):
    """A verifier of one manifest attestation."""

    def __call__(
        self,
        policy: AttestationPolicy,
        artifact: Path,
        bundle: Path,
        *,
        timeout_seconds: float | None = None,
    ) -> object:
        """Verify one attestation, bounded by the remaining budget."""
        ...


@dc.dataclass(frozen=True, slots=True)
class InstallOptions:
    """One reviewed snapshot and the transport that supplies its assets.

    Attributes
    ----------
    pin : SnapshotPin
        Reviewed consumer pin.
    assets_dir : Path | None
        Local directory holding the release files. Remote transport is not
        available until the producer release exists.
    cache_home : Path | None
        Explicit cache root. Defaults to `XDG_CACHE_HOME`, then `~/.cache`.
    lock_timeout_seconds : float
        Bounded wait for the per-identity installation lock.
    deadline_seconds : float
        Overall installation budget measured with a monotonic clock.
    """

    pin: SnapshotPin
    assets_dir: Path | None = None
    cache_home: Path | None = None
    lock_timeout_seconds: float = 300.0
    deadline_seconds: float = 30 * 60.0

    def __post_init__(self) -> None:
        """Reject a budget that cannot bound the work."""
        if self.lock_timeout_seconds < 0:
            msg = "snapshot lock timeout must not be negative"
            raise ProverToolError(msg)
        if self.deadline_seconds < 0:
            msg = "snapshot install deadline must not be negative"
            raise ProverToolError(msg)


@dc.dataclass(frozen=True, slots=True)
class InstallHooks:
    """External operations the installation pipeline depends on.

    Attributes
    ----------
    setup : SetupRunner
        Runs the packaged launcher's local-bundle setup. The third argument is
        the remaining installation budget, or `None` when unbounded.
    probe : ProbeRunner
        Observes the compiler identities and toolchain link after setup. The
        second argument is the remaining budget, or `None` when unbounded.
    verify_attestation : AttestationVerifier
        Verifies the manifest attestation before any extraction.
    """

    setup: SetupRunner = run_local_bundle_setup
    probe: ProbeRunner = probe_generation
    verify_attestation: AttestationVerifier = verify_attestation
