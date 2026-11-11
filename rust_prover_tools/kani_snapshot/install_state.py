"""Activation state, locking and cache revalidation for snapshots.

An active generation is reusable only while its receipt, cached payloads,
toolchain link and effective compiler all still match the reviewed manifest.
Readers never mutate a generation; installation replaces one pointer atomically
under a per-identity advisory lock.
"""

from __future__ import annotations

import contextlib
import fcntl
import functools
import os
import time
import typing as typ

from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_snapshot.activation import (
    ActivePointer,
    load_active,
    write_active,
)
from rust_prover_tools.kani_snapshot.durability import fsync_directory
from rust_prover_tools.kani_snapshot.identity import (
    SnapshotIdentity,
    build_identity_report,
    identity_directory,
    runtime_for,
)
from rust_prover_tools.kani_snapshot.install_cache import (
    validate_cached_payloads,
    validate_installed_tree,
)
from rust_prover_tools.kani_snapshot.install_model import (
    MANIFEST_ASSET_NAME,
    InstallOptions,
    ProbeRunner,
)
from rust_prover_tools.kani_snapshot.manifest import (
    parse_manifest,
    validate_binding,
)
from rust_prover_tools.kani_snapshot.publication import (
    prune_superseded_markers,
    publication_pending,
)
from rust_prover_tools.kani_snapshot.receipt import (
    load_receipt,
    validate_receipt,
)
from rust_prover_tools.kani_snapshot.toolchain_binding import verify_toolchain

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

    from rust_prover_tools.kani_snapshot.install_model import InstallBudget
    from rust_prover_tools.kani_snapshot.models import (
        SnapshotManifest,
        SnapshotPin,
    )
    from rust_prover_tools.kani_snapshot.receipt_model import SetupEvidence
    from rust_prover_tools.kani_snapshot.snapshot_layout import (
        SnapshotLayout,
        SnapshotRuntime,
    )

__all__ = (
    "ActivePointer",
    "active_manifest",
    "ensure_private_directory",
    "fsync_directory",
    "identity_lock",
    "load_active",
    "prune_superseded_markers",
    "publication_pending",
    "require_runtime_files",
    "validate_generation",
    "write_active",
)

_PRIVATE_DIRECTORY_MODE: typ.Final[int] = 0o700
_PRIVATE_FILE_MODE: typ.Final[int] = 0o600
_LOCK_POLL_SECONDS: typ.Final[float] = 0.05
_EXECUTABLE_BITS: typ.Final[int] = 0o111


@contextlib.contextmanager
def identity_lock(
    options: InstallOptions,
    *,
    timeout: float,
    budget: InstallBudget | None = None,
    read_only: bool = False,
) -> cabc.Generator[None]:
    """Hold the per-identity advisory lock, waiting at most `timeout`.

    The lock file is created private. A timeout is reported as a snapshot
    error rather than retried indefinitely, so a stuck owner cannot hang a
    proof forever. A `budget` bounds the directory creation, the lock file's
    creation and the wait itself by the seconds the caller still has, so work
    already done under that budget is not spent twice. The wait never exceeds
    what remains: a lock acquired at the last second would leave no time for
    the installation it protects.

    In `read_only` mode the lock is shared and the cache is never created: a
    missing directory or lock file means no installation has ever run for this
    identity, so there is no state an installer could be mutating and nothing
    to synchronize with. When the lock exists, a shared acquisition waits for
    any exclusive installation or repair holding it, so a check never reads a
    half-repaired cache.
    """
    identity_dir = identity_directory(options.pin, cache_home=options.cache_home)
    path = identity_dir / "install.lock"
    if read_only:
        descriptor = _open_existing_lock_file(path, budget)
        if descriptor is None:
            yield
            return
        _hold_lock(descriptor, timeout, budget, shared=True)
    else:
        _create_identity_directory(identity_dir, budget)
        descriptor = _open_lock_file(path, budget)
        _hold_lock(descriptor, timeout, budget, shared=False)
    try:
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _hold_lock(
    descriptor: int,
    timeout: float,
    budget: InstallBudget | None,
    *,
    shared: bool,
) -> None:
    """Acquire the descriptor, releasing it if the wait fails."""
    try:
        _await_lock(descriptor, timeout, budget, shared=shared)
    except BaseException:
        with contextlib.suppress(OSError):
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)
        raise


def _create_identity_directory(
    directory: Path,
    budget: InstallBudget | None,
) -> None:
    """Create the identity directory, abandoning a stalled creation.

    The directory is private and its creation is idempotent, so a creation
    that outlives the budget may finish later without changing any decision
    this call makes.
    """
    if budget is None:
        ensure_private_directory(directory)
    else:
        budget.supervise(
            "identity directory creation",
            functools.partial(ensure_private_directory, directory),
        )


def _open_lock_file(path: Path, budget: InstallBudget | None) -> int:
    """Open the lock file private, bounded by `budget` when supplied."""

    def open_private() -> int:
        """Open or create the lock file descriptor.

        A worker that finishes after the budget expired closes its own
        descriptor: the caller has already stopped waiting for the value, so
        nobody else could close it.
        """
        descriptor = os.open(
            path,
            os.O_RDWR | os.O_CREAT | os.O_CLOEXEC,
            _PRIVATE_FILE_MODE,
        )
        if budget is not None and budget.remaining() <= 0:
            os.close(descriptor)
            msg = "snapshot installation deadline expired during lock file creation"
            raise ProverToolError(msg)
        return descriptor

    try:
        if budget is None:
            return open_private()
        return budget.supervise("lock file creation", open_private)
    except OSError as exc:
        msg = f"failed to open snapshot install lock '{path}': {exc}"
        raise ProverToolError(msg) from exc


def _open_existing_lock_file(path: Path, budget: InstallBudget | None) -> int | None:
    """Open an existing lock file read-only, or report that none exists.

    A read-only caller must not create the cache, so a missing file is an
    ordinary absence rather than an error.
    """

    def open_existing() -> int | None:
        """Open the lock file without creating it, or return `None`."""
        try:
            return os.open(path, os.O_RDONLY | os.O_CLOEXEC)
        except FileNotFoundError:
            return None
        except OSError as exc:
            msg = f"failed to open snapshot install lock '{path}': {exc}"
            raise ProverToolError(msg) from exc

    if budget is None:
        return open_existing()
    return budget.supervise("lock file open", open_existing)


def _await_lock(
    descriptor: int,
    timeout: float,
    budget: InstallBudget | None,
    *,
    shared: bool = False,
) -> None:
    """Acquire the lock, waiting no longer than `timeout` or the budget."""
    wait = timeout if budget is None else min(timeout, budget.remaining())
    deadline = time.monotonic() + wait
    mode = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
    while True:
        try:
            fcntl.flock(descriptor, mode | fcntl.LOCK_NB)
        except BlockingIOError:
            if time.monotonic() >= deadline:
                msg = "timed out waiting for the snapshot installation lock"
                raise ProverToolError(msg) from None
            time.sleep(_LOCK_POLL_SECONDS)
        else:
            return


def ensure_private_directory(path: Path) -> None:
    """Create one cache directory with private permissions.

    Every missing level is created explicitly. `Path.mkdir(parents=True)` gives
    intermediate directories the default mode masked by the umask, so a umask
    of `0002` would leave a `0o775` cache parent that later extraction refuses
    as attacker-writable. The final `mkdir` still rejects an existing
    non-directory target.
    """
    try:
        missing = [entry for entry in path.parents if not entry.exists()]
        for directory in reversed(missing):
            directory.mkdir(mode=_PRIVATE_DIRECTORY_MODE, exist_ok=True)
        path.mkdir(mode=_PRIVATE_DIRECTORY_MODE, exist_ok=True)
    except OSError as exc:
        msg = f"failed to create snapshot cache directory '{path}': {exc}"
        raise ProverToolError(msg) from exc


def active_manifest(pin: SnapshotPin, layout: SnapshotLayout) -> SnapshotManifest:
    """Load and verify the manifest retained beside the cached payloads."""
    path = layout.downloads_dir / MANIFEST_ASSET_NAME
    try:
        data = path.read_bytes()
    except OSError as exc:
        msg = f"snapshot cache has no readable manifest '{path}': {exc}"
        raise ProverToolError(msg) from exc
    parsed = parse_manifest(data)
    if parsed.sha256 != pin.manifest_sha256:
        msg = "snapshot manifest digest does not match the reviewed pin"
        raise ProverToolError(msg)
    validate_binding(pin, parsed.manifest)
    return parsed.manifest


# pylint: disable-next=too-many-arguments,too-many-positional-arguments
def validate_generation(  # ruff: ignore[too-many-arguments, too-many-positional-arguments] - one parameter per validated input
    options: InstallOptions,
    manifest: SnapshotManifest,
    layout: SnapshotLayout,
    probe: ProbeRunner,
    remaining_budget: cabc.Callable[[], float] | None = None,
    *,
    checkpoint: cabc.Callable[[], None] | None = None,
) -> SnapshotIdentity:
    """Return the identity of a generation that still satisfies the manifest.

    Parameters
    ----------
    options : InstallOptions
        Reviewed pin and cache root.
    manifest : SnapshotManifest
        Manifest the generation was installed from.
    layout : SnapshotLayout
        Generation to validate.
    probe : ProbeRunner
        Compiler identity probe.
    remaining_budget : collections.abc.Callable[[], float] | None
        Called immediately before the probe to obtain the seconds left in
        the installation budget, so slow receipt and cache checks cannot
        spend the probe's share. `None` leaves the probe unbounded.
    checkpoint : collections.abc.Callable[[], None] | None
        Called while cached payloads and the installed tree are verified, so an
        expired budget interrupts a long validation.

    Raises
    ------
    ProverToolError
        Raised when the receipt, cached payloads, toolchain link or effective
        compiler no longer match the reviewed snapshot, or when the probe
        exceeds the remaining budget.
    """
    receipt = load_receipt(layout.receipt_path)
    validate_receipt(receipt, options.pin, manifest, layout)
    runtime = runtime_for(layout, manifest)
    require_runtime_files(runtime)
    _check_toolchain_link(runtime, receipt.toolchain_link_target)
    validate_cached_payloads(layout, manifest, checkpoint=checkpoint)
    validate_installed_tree(layout, manifest, runtime, checkpoint=checkpoint)
    verify_toolchain(runtime, manifest, checkpoint=checkpoint)
    budget = None if remaining_budget is None else remaining_budget()
    evidence = probe(runtime, budget)
    _check_evidence(evidence, manifest, runtime)
    if evidence.observed != receipt.observed:
        msg = "snapshot receipt observation does not match the installed compiler"
        raise ProverToolError(msg)
    return build_identity_report(options.pin, manifest, runtime)


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
