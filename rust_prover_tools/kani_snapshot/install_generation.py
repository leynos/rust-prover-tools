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
from rust_prover_tools.kani_snapshot.cleanup import discard_private_file
from rust_prover_tools.kani_snapshot.durability import (
    flush_directory,
    flush_tree,
    fsync_directory,
    fsync_file,
)
from rust_prover_tools.kani_snapshot.inventory import load_inventory
from rust_prover_tools.kani_snapshot.inventory_model import (
    KIND_DIRECTORY,
    KIND_FILE,
    KIND_HARDLINK,
)
from rust_prover_tools.kani_snapshot.progress import as_checkpoint
from rust_prover_tools.kani_snapshot.toolchain_binding import toolchain_root

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
    "flush_generation_entries",
    "install_launcher_binaries",
    "require_evidence",
    "write_generation_file",
)

_PRIVATE_FILE_MODE: typ.Final[int] = 0o600
#: Rustup's record of the components installed in one toolchain.
_TOOLCHAIN_COMPONENTS: typ.Final[str] = "lib/rustlib/components"


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
    # The launchers are new entries in `bin`, so that directory must be
    # durable before the pointer can name a generation that runs them.
    flush_directory(
        target,
        budget=budget,
        phase="launcher directory flush",
    )


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
            # The mode is applied to the open descriptor before the fsync so
            # the executable bit is as durable as the bytes it guards.
            os.fchmod(writer.fileno(), mode)
            writer.flush()
            os.fsync(writer.fileno())

    try:
        if budget is None:
            copy()
        else:
            budget.supervise("launcher placement", copy)
    except OSError as exc:
        discard_private_file(
            destination,
            budget=budget,
            phase="launcher cleanup",
        )
        msg = f"failed to install snapshot launcher '{entry.name}': {exc}"
        raise ProverToolError(msg) from exc
    except ProverToolError:
        discard_private_file(
            destination,
            budget=budget,
            phase="launcher cleanup",
        )
        raise


def flush_generation_entries(
    runtime: SnapshotRuntime,
    layout: SnapshotLayout,
    manifest: SnapshotManifest,
    *,
    budget: InstallBudget | None = None,
) -> None:
    """Make every required generation entry durable before activation.

    Setup writes most of the generation's tree, so the installer flushes the
    state its own contract requires: the executables a consumer runs, the
    toolchain compiler, component record and every recorded library, every
    file and directory the backend inventory publishes, the directories that
    name them, and every member of the setup-created Python root. A crash
    therefore cannot leave a durable activation record naming a generation
    whose launcher, library, backend member or Python payload was never
    written back. Flushing the compiled binaries alone is not enough because a
    file's parent directory entry is what makes the file reachable after a
    crash, and an inventory member such as `share/data.txt` is as load-bearing
    as a binary even though no manifest field names it individually.

    Files are flushed before their directories, and directories are flushed
    deepest first, so every entry is durable before the entry that names it.
    Setup's remaining internal data is outside the installer's control; these
    are the files and entries this pipeline requires.
    """
    toolchain = toolchain_root(runtime)
    toolchain_files = (
        runtime.rustc,
        toolchain / _TOOLCHAIN_COMPONENTS,
        *(toolchain / library.path for library in manifest.toolchain.libraries),
    )
    backend_files, backend_directories = _backend_inventory_entries(
        runtime,
        layout,
        manifest,
        budget=budget,
    )
    files = (
        runtime.launcher,
        runtime.driver,
        runtime.compiler,
        *toolchain_files,
        *backend_files,
    )
    for path in files:
        fsync_file(
            path,
            budget=budget,
            phase="generation file flush",
        )
    directories = _fixed_directories(runtime, layout, toolchain)
    directories.update(backend_directories)
    directories.update(_ancestor_directories(backend_files, runtime.kani_dir))
    directories.update(_ancestor_directories(toolchain_files, toolchain))
    for directory in sorted(
        directories,
        key=lambda path: len(path.parts),
        reverse=True,
    ):
        flush_directory(
            directory,
            budget=budget,
            phase="generation directory flush",
        )
    flush_tree(runtime.python_root, budget=budget, phase="Python root flush")


def _backend_inventory_entries(
    runtime: SnapshotRuntime,
    layout: SnapshotLayout,
    manifest: SnapshotManifest,
    *,
    budget: InstallBudget | None = None,
) -> tuple[tuple[Path, ...], tuple[Path, ...]]:
    """Return the published backend's files and directories, absolute.

    Setup extracts the backend archive, so its inventory is the closed list of
    payload members the generation must keep. Symbolic links are deliberately
    not returned as files: flushing one would open its target, and that target
    is either another recorded member or outside this generation.
    """
    # `install_transport` imports `publication`, which imports this module, so
    # the asset lookup is imported at call time to keep the graph acyclic.
    from rust_prover_tools.kani_snapshot.install_transport import asset_by_role

    read = functools.partial(
        load_inventory,
        layout.downloads_dir / asset_by_role(manifest, "inventory").name,
    )
    # Reading the cached inventory is a local read, so abandoning it at the
    # deadline changes nothing a later attempt would observe.
    inventory = read() if budget is None else budget.supervise("inventory read", read)
    files = tuple(
        runtime.kani_dir / entry.path
        for entry in inventory.entries
        if entry.kind in {KIND_FILE, KIND_HARDLINK}
    )
    directories = tuple(
        runtime.kani_dir / entry.path
        for entry in inventory.entries
        if entry.kind == KIND_DIRECTORY
    )
    return files, directories


def _fixed_directories(
    runtime: SnapshotRuntime,
    layout: SnapshotLayout,
    toolchain: Path,
) -> set[Path]:
    """Return the directories required regardless of inventory content."""
    return {
        runtime.kani_dir / "bin",
        runtime.kani_dir,
        layout.kani_home,
        layout.rustup_home / "toolchains",
        layout.rustup_home,
        layout.generation_dir,
        toolchain,
        toolchain / "bin",
        toolchain / "lib",
    }


def _ancestor_directories(files: cabc.Iterable[Path], root: Path) -> set[Path]:
    """Return every directory between each file and `root`, inclusive.

    A file's parent directory entry is what makes the file reachable after a
    crash, so a required file makes its whole ancestor chain required. The
    chain stops at `root`, so a manifest path that escapes the tree cannot add
    a directory outside it.
    """
    directories: set[Path] = set()
    for path in files:
        parent = path.parent
        while parent.is_relative_to(root):
            directories.add(parent)
            if parent == root:
                break
            parent = parent.parent
    return directories


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
    budget expires. Two callers write identity-level shared state instead: the
    publication marker, whose whole purpose is to be visible before the first
    payload replacement, and the completion record, whose name carries the
    sequence it completes so a late record joins the order beneath a newer one
    rather than replacing it.
    """
    pending = path.with_name(
        f".{path.name}-{os.getpid()}-{time.monotonic_ns()}",
    )

    def write() -> None:
        """Write the pending file and make it durable."""
        with pending.open("xb") as handle:
            handle.write(data)
            os.fchmod(handle.fileno(), _PRIVATE_FILE_MODE)
            handle.flush()
            os.fsync(handle.fileno())
        pending.replace(path)
        fsync_directory(path.parent)

    try:
        if budget is None:
            write()
        else:
            budget.supervise(description, write)
    except OSError as exc:
        discard_private_file(pending, budget=budget, phase="generation file cleanup")
        msg = f"failed to write snapshot {description}: {exc}"
        raise ProverToolError(msg) from exc
    except ProverToolError:
        discard_private_file(pending, budget=budget, phase="generation file cleanup")
        raise
