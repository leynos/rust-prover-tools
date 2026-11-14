"""Filesystem validation and compiler verification of one generation.

The two halves are deliberately separate entry points. `validate_generation`
reads the receipt, cached payloads, installed tree and toolchain bytes and
starts no process. `verify_effective_compiler` runs the generation's compilers
through the configured admission route and compares what they report with the
receipt. A caller that only needs to know whether the installed bytes are
intact therefore never invokes a compiler wrapper.
"""

from __future__ import annotations

import dataclasses as dc
import typing as typ

from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_snapshot.identity import (
    SnapshotIdentity,
    build_identity_report,
    runtime_for,
)
from rust_prover_tools.kani_snapshot.install_cache import (
    validate_cached_payloads,
    validate_installed_tree,
)
from rust_prover_tools.kani_snapshot.receipt import (
    load_receipt,
    validate_receipt,
)
from rust_prover_tools.kani_snapshot.toolchain_binding import verify_toolchain

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

    from rust_prover_tools.kani_snapshot.install_model import (
        InstallOptions,
        ProbeRunner,
    )
    from rust_prover_tools.kani_snapshot.models import SnapshotManifest
    from rust_prover_tools.kani_snapshot.receipt_model import (
        ObservedToolchain,
        SetupEvidence,
    )
    from rust_prover_tools.kani_snapshot.snapshot_layout import (
        SnapshotLayout,
        SnapshotRuntime,
    )

__all__ = (
    "ValidatedGeneration",
    "require_runtime_files",
    "validate_generation",
    "verify_effective_compiler",
)

_EXECUTABLE_BITS: typ.Final[int] = 0o111


@dc.dataclass(frozen=True, slots=True)
class ValidatedGeneration:
    """Filesystem state that a generation must still satisfy.

    This is the result of the process-free query. It records the identities
    the receipt claims, so `verify_effective_compiler` can compare a fresh
    probe against them without re-reading the cache.

    Attributes
    ----------
    manifest : SnapshotManifest
        Manifest the generation was installed from.
    runtime : SnapshotRuntime
        Generation and toolchain the validation resolved.
    identity : SnapshotIdentity
        Identity report the validation established.
    receipt_observation : ObservedToolchain
        Compiler identities the receipt recorded at installation time.
    """

    manifest: SnapshotManifest
    runtime: SnapshotRuntime
    identity: SnapshotIdentity
    receipt_observation: ObservedToolchain


def validate_generation(
    options: InstallOptions,
    manifest: SnapshotManifest,
    layout: SnapshotLayout,
    *,
    checkpoint: cabc.Callable[[], None] | None = None,
) -> ValidatedGeneration:
    """Validate a generation's files without running any process.

    Receipt, manifest binding, runtime files, toolchain link, cached payloads,
    the installed tree and the toolchain bytes are all checked here. The
    effective compiler is not: proving it needs `rustc -vV` and
    `kani-compiler --version`, which `verify_effective_compiler` runs
    explicitly. Keeping the two apart lets a caller answer "are the installed
    bytes intact?" without a process, a wrapper the operator configured, or
    its side effects.

    Parameters
    ----------
    options : InstallOptions
        Reviewed pin and cache root.
    manifest : SnapshotManifest
        Manifest the generation was installed from.
    layout : SnapshotLayout
        Generation to validate.
    checkpoint : collections.abc.Callable[[], None] | None
        Called while cached payloads and the installed tree are verified, so an
        expired budget interrupts a long validation.

    Raises
    ------
    ProverToolError
        Raised when the receipt, cached payloads, toolchain link or installed
        tree no longer match the reviewed snapshot.
    """
    receipt = load_receipt(layout.receipt_path)
    validate_receipt(receipt, options.pin, manifest, layout)
    runtime = runtime_for(layout, manifest)
    require_runtime_files(runtime)
    _check_toolchain_link(runtime, receipt.toolchain_link_target)
    validate_cached_payloads(layout, manifest, checkpoint=checkpoint)
    validate_installed_tree(layout, manifest, runtime, checkpoint=checkpoint)
    verify_toolchain(runtime, manifest, checkpoint=checkpoint)
    return ValidatedGeneration(
        manifest=manifest,
        runtime=runtime,
        identity=build_identity_report(options.pin, manifest, runtime),
        receipt_observation=receipt.observed,
    )


def verify_effective_compiler(
    validated: ValidatedGeneration,
    probe: ProbeRunner,
    remaining_budget: cabc.Callable[[], float] | None = None,
) -> SnapshotIdentity:
    """Run the compiler probe and require the observed identities to match.

    This function executes processes: the `probe` runs the generation's
    `rustc -vV` and `kani-compiler --version` through the configured admission
    route, which may let a configured wrapper write to its own caches. It is
    therefore separate from `validate_generation`, which touches files only.

    Parameters
    ----------
    validated : ValidatedGeneration
        Result of the filesystem validation the probe must corroborate.
    probe : ProbeRunner
        Compiler identity probe.
    remaining_budget : collections.abc.Callable[[], float] | None
        Called immediately before the probe to obtain the seconds left in the
        installation budget, so slow receipt and cache checks cannot spend the
        probe's share. `None` leaves the probe unbounded.

    Returns
    -------
    SnapshotIdentity
        The identity report of the validated generation.

    Raises
    ------
    ProverToolError
        Raised when the observed identities, the toolchain link target or the
        receipt observation no longer match the reviewed snapshot, or when the
        probe exceeds the remaining budget.
    """
    budget = None if remaining_budget is None else remaining_budget()
    evidence = probe(validated.runtime, budget)
    _check_evidence(evidence, validated.manifest, validated.runtime)
    if evidence.observed != validated.receipt_observation:
        msg = "snapshot receipt observation does not match the installed compiler"
        raise ProverToolError(msg)
    return validated.identity


def require_runtime_files(runtime: SnapshotRuntime) -> None:
    """Require every executable the runtime will invoke to be present.

    Installation and revalidation both call this before a generation may
    become active, so a launcher archive that ships `cargo-kani` for setup but
    omits `kani` cannot be published as usable.
    """
    paths = (
        ("launcher", runtime.launcher),
        ("driver", runtime.driver),
        ("compiler", runtime.compiler),
        ("rustc", runtime.rustc),
    )
    for name, path in paths:
        try:
            status = path.stat()
        except OSError as exc:
            msg = f"snapshot {name} is missing: {exc}"
            raise ProverToolError(msg) from exc
        if not path.is_file() or not status.st_mode & _EXECUTABLE_BITS:
            msg = f"snapshot {name} is not an executable regular file: {path}"
            raise ProverToolError(msg)


def _check_toolchain_link(runtime: SnapshotRuntime, expected: Path) -> None:
    """Compare the live toolchain symlink with the receipt."""
    link = runtime.kani_dir / "toolchain"
    try:
        target = link.readlink()
    except OSError as exc:
        msg = f"snapshot toolchain link is missing: {exc}"
        raise ProverToolError(msg) from exc
    if not target.is_absolute() or target != expected:
        msg = f"snapshot toolchain link does not match the receipt: {target}"
        raise ProverToolError(msg)


def _check_evidence(
    evidence: SetupEvidence,
    manifest: SnapshotManifest,
    runtime: SnapshotRuntime,
) -> None:
    """Require observed identities to equal the reviewed manifest."""
    expected_link = runtime.layout.rustup_home / "toolchains" / runtime.toolchain
    checks = (
        (
            "rustc",
            evidence.observed.rustc_vv,
            manifest.toolchain.rustc_vv,
        ),
        (
            "kani-compiler",
            evidence.observed.kani_compiler,
            manifest.toolchain.kani_compiler,
        ),
        ("toolchain link", evidence.toolchain_link_target, expected_link),
    )
    for name, actual, wanted in checks:
        if actual != wanted:
            msg = f"snapshot {name} identity does not match the manifest"
            raise ProverToolError(msg)
