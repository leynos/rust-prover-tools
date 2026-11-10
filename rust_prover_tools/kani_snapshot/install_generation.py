"""Generation-local writes performed by one installation.

Most helpers here publish a file that only the new, still unreferenced
generation can see: the launcher binaries placed beside the backend, the
Python-root record, and the receipt. None of them is shared state until the
active pointer moves, so a write stalled on a hung filesystem may be abandoned
when the installation budget expires. `write_generation_file` also publishes
the identity-level publication marker, which is deliberately shared: its job is
to make an unconfirmed payload publication visible to readers. The evidence
check also lives here because it decides whether that generation may be
recorded at all.
"""

from __future__ import annotations

import functools
import os
import stat
import time
import typing as typ

from rust_prover_tools.download_stream import READ_CHUNK_SIZE
from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_snapshot.activation import fsync_directory
from rust_prover_tools.kani_snapshot.progress import as_checkpoint

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

    from rust_prover_tools.kani_snapshot.install_model import InstallBudget
    from rust_prover_tools.kani_snapshot.models import SnapshotManifest
    from rust_prover_tools.kani_snapshot.receipt_model import SetupEvidence
    from rust_prover_tools.kani_snapshot.snapshot_layout import (
        SnapshotLayout,
        SnapshotRuntime,
    )

__all__ = (
    "copy_launcher",
    "install_launcher_binaries",
    "require_evidence",
    "write_generation_file",
)

_PRIVATE_FILE_MODE: typ.Final[int] = 0o600


def install_launcher_binaries(
    runtime: SnapshotRuntime,
    layout: SnapshotLayout,
    *,
    budget: InstallBudget | None = None,
) -> None:
    """Place the packaged launcher beside the backend binaries."""
    source = layout.launcher_dir / "bin"
    target = runtime.kani_dir / "bin"
    target.mkdir(parents=True, exist_ok=True)
    checkpoint = (
        None
        if budget is None
        else functools.partial(budget.check, "launcher placement")
    )
    for entry in sorted(source.iterdir()):
        if checkpoint is not None:
            checkpoint()
        destination = target / entry.name
        if destination.exists():
            continue
        copy_launcher(entry, destination, checkpoint, budget)


def copy_launcher(
    entry: Path,
    destination: Path,
    checkpoint: cabc.Callable[[], None] | None,
    budget: InstallBudget | None = None,
) -> None:
    """Copy one launcher file in bounded chunks that report progress.

    `shutil.copy2` is one uninterrupted transfer, so a launcher archive with a
    large member could spend the rest of the installation budget inside it
    without ever reaching a checkpoint. Each copy is also supervised, because a
    stalled `read`, `write` or `fsync` never reaches a checkpoint at all. The
    copy preserves the archive member's permission bits, which the launcher
    contract then requires.
    """
    progress = as_checkpoint(checkpoint)

    def copy() -> None:
        """Transfer one launcher file and make it durable."""
        mode = stat.S_IMODE(entry.stat().st_mode)
        with entry.open("rb") as reader, destination.open("xb") as writer:
            while chunk := reader.read(READ_CHUNK_SIZE):
                progress()
                writer.write(chunk)
            writer.flush()
            os.fsync(writer.fileno())
        destination.chmod(mode)

    try:
        if budget is None:
            copy()
        else:
            budget.supervise("launcher placement", copy)
    except OSError as exc:
        destination.unlink(missing_ok=True)
        msg = f"failed to install snapshot launcher '{entry.name}': {exc}"
        raise ProverToolError(msg) from exc
    except ProverToolError:
        destination.unlink(missing_ok=True)
        raise


def require_evidence(
    evidence: SetupEvidence,
    manifest: SnapshotManifest,
    runtime: SnapshotRuntime,
) -> None:
    """Require setup to have produced the reviewed compiler identity."""
    expected_link = runtime.layout.rustup_home / "toolchains" / runtime.toolchain
    checks = (
        ("rustc", evidence.observed.rustc_vv, manifest.toolchain.rustc_vv),
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


def write_generation_file(
    path: Path,
    data: bytes,
    description: str,
    *,
    budget: InstallBudget | None = None,
) -> None:
    """Publish one small file durably beside its final path.

    Generation-local callers write files that are private until the pointer
    names their generation, so a stalled write may be abandoned when the
    budget expires. The publication marker is the deliberate exception: it is
    identity-level shared state whose whole purpose is to be visible before
    the first payload replacement.
    """
    pending = path.with_name(
        f".{path.name}-{os.getpid()}-{time.monotonic_ns()}",
    )

    def write() -> None:
        """Write the pending file and make it durable."""
        with pending.open("xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        pending.chmod(_PRIVATE_FILE_MODE)
        pending.replace(path)
        fsync_directory(path.parent)

    try:
        if budget is None:
            write()
        else:
            budget.supervise(description, write)
    except OSError as exc:
        pending.unlink(missing_ok=True)
        msg = f"failed to write snapshot {description}: {exc}"
        raise ProverToolError(msg) from exc
    except ProverToolError:
        pending.unlink(missing_ok=True)
        raise
