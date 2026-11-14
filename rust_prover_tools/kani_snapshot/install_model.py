"""Options and injectable boundaries for snapshot installation.

The pipeline is deliberately split from the external tools it drives. Tests
supply a setup runner, a compiler probe and an attestation verifier, while these
defaults execute the packaged launcher and `gh`.
"""

from __future__ import annotations

import collections.abc as cabc
import dataclasses as dc
import functools
import time
import typing as typ

from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_snapshot.attestation import verify_attestation
from rust_prover_tools.kani_snapshot.install_defaults import (
    probe_host_generation,
    run_local_bundle_setup,
)
from rust_prover_tools.kani_snapshot.progress import run_within

if typ.TYPE_CHECKING:
    from pathlib import Path

    from rust_prover_tools.kani_snapshot.attestation import AttestationRequest
    from rust_prover_tools.kani_snapshot.identity import SnapshotRuntime
    from rust_prover_tools.kani_snapshot.models import SnapshotPin
    from rust_prover_tools.kani_snapshot.receipt_model import SetupEvidence

_T = typ.TypeVar("_T")

__all__ = (
    "ATTESTATION_ASSET_NAME",
    "CHECKSUMS_ASSET_NAME",
    "MANIFEST_ASSET_NAME",
    "AttestationVerifier",
    "ClaimedBudget",
    "Deadline",
    "InstallBudget",
    "InstallHooks",
    "InstallOptions",
    "ProbeRunner",
    "SetupRunner",
    "StagingGuard",
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

    def supervise(
        self,
        phase: str,
        operation: cabc.Callable[[], _T],
    ) -> _T:
        """Run one possibly blocking local operation under the budget."""


@dc.dataclass(slots=True)
class Deadline:
    """A monotonic installation budget shared by every phase.

    The concrete implementation lives beside the protocol it satisfies so the
    phase module that orchestrates installation does not also own clock
    arithmetic.
    """

    expires_at: float

    def check(self, phase: str) -> None:
        """Fail when the budget has expired."""
        if time.monotonic() >= self.expires_at:
            msg = f"snapshot installation deadline expired during {phase}"
            raise ProverToolError(msg)

    def remaining(self) -> float:
        """Return the seconds left, never negative."""
        return max(0.0, self.expires_at - time.monotonic())

    def checkpoint(self, phase: str) -> cabc.Callable[[], None]:
        """Return a callable that enforces the budget inside `phase`.

        Long local work such as copying, hashing, archive extraction and tree
        validation calls it repeatedly, so an expired budget stops the phase
        rather than being noticed only at the next phase boundary.
        """
        return functools.partial(self.check, phase)

    def supervise(
        self,
        phase: str,
        operation: cabc.Callable[[], _T],
    ) -> _T:
        """Run one blocking local operation without exceeding the budget."""
        return run_within(operation, remaining=self.remaining, phase=phase)


class StagingGuard(typ.Protocol):
    """A claim a supervised worker holds while it may use a staging tree."""

    def guard(self, operation: cabc.Callable[[], _T]) -> cabc.Callable[[], _T]:
        """Wrap `operation` so its worker holds the claim until it stops."""

    def finish(self) -> bool:
        """Release the claim, reporting whether no worker can still use it."""


@dc.dataclass(frozen=True, slots=True)
class ClaimedBudget:
    """An installation budget whose supervised operations hold a claim.

    Every operation this budget supervises may outlive the caller that
    abandoned it, so each one is wrapped with `StagingGuard.guard`. A helper
    that supervises its own work, such as payload staging or publication, can
    therefore extend the claim without changing its signature.
    """

    budget: InstallBudget
    claim: StagingGuard

    def check(self, phase: str) -> None:
        """Raise when the shared budget has expired during `phase`."""
        self.budget.check(phase)

    def remaining(self) -> float:
        """Return the seconds the shared budget has left."""
        return self.budget.remaining()

    def supervise(
        self,
        phase: str,
        operation: cabc.Callable[[], _T],
    ) -> _T:
        """Run one possibly blocking operation under the claim and budget."""
        return self.budget.supervise(phase, self.claim.guard(operation))

    def finish(self) -> bool:
        """Release the claim, reporting whether its tree may be removed."""
        return self.claim.finish()


class AttestationVerifier(typ.Protocol):
    """A verifier of one manifest attestation."""

    def __call__(
        self,
        request: AttestationRequest,
        *,
        timeout_seconds: float | None = None,
    ) -> object:
        """Verify one attestation request within the remaining budget."""
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
        second argument is the remaining budget, or `None` when unbounded. The
        default runs `probe_host_generation`, the composition point that reads
        the host environment; tests inject a probe instead.
    verify_attestation : AttestationVerifier
        Verifies the manifest attestation before any extraction.
    """

    setup: SetupRunner = run_local_bundle_setup
    probe: ProbeRunner = probe_host_generation
    verify_attestation: AttestationVerifier = verify_attestation
