"""Activation state, locking and cache revalidation for snapshots.

An active generation is reusable only while its receipt, cached payloads,
toolchain link and effective compiler all still match the reviewed manifest.
Readers never mutate a generation; installation replaces one pointer atomically
under a per-identity advisory lock.
"""

from __future__ import annotations

import contextlib
import dataclasses as dc
import fcntl
import json
import os
import time
import typing as typ

from rust_prover_tools.errors import ProverToolError
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
from rust_prover_tools.kani_snapshot.models import SCHEMA_VERSION
from rust_prover_tools.kani_snapshot.receipt import (
    load_receipt,
    validate_receipt,
)
from rust_prover_tools.kani_snapshot.snapshot_layout import (
    SnapshotLayout,
    SnapshotRuntime,
    is_safe_generation_name,
)
from rust_prover_tools.kani_snapshot.strict_json import strict_json_loads
from rust_prover_tools.kani_snapshot.validation import (
    require_int,
    require_mapping,
    require_truthy_text,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

    from rust_prover_tools.kani_snapshot.models import (
        SnapshotManifest,
        SnapshotPin,
    )
    from rust_prover_tools.kani_snapshot.receipt_model import SetupEvidence

__all__ = (
    "ActivePointer",
    "active_manifest",
    "ensure_private_directory",
    "fsync_directory",
    "identity_lock",
    "load_active",
    "validate_generation",
    "write_active",
)

_ACTIVE_KEYS: typ.Final[frozenset[str]] = frozenset({
    "schema_version",
    "generation",
})
_PRIVATE_DIRECTORY_MODE: typ.Final[int] = 0o700
_PRIVATE_FILE_MODE: typ.Final[int] = 0o600
_LOCK_POLL_SECONDS: typ.Final[float] = 0.05
_EXECUTABLE_BITS: typ.Final[int] = 0o111


@dc.dataclass(frozen=True, slots=True)
class ActivePointer:
    """The atomically replaced pointer naming the active generation.

    Attributes
    ----------
    schema_version : int
        Pointer schema version. Only version 1 is accepted.
    generation : str
        One safe generation directory name.
    """

    schema_version: int
    generation: str


def load_active(layout: SnapshotLayout) -> ActivePointer | None:
    """Read the active pointer, or return `None` when none exists."""
    try:
        data = layout.active_path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        msg = f"failed to read snapshot active pointer: {exc}"
        raise ProverToolError(msg) from exc
    document = require_mapping(
        strict_json_loads(data, "active pointer"),
        "active pointer document",
    )
    unknown = sorted(set(document) - _ACTIVE_KEYS)
    if unknown:
        msg = f"snapshot active pointer has unknown keys: {', '.join(unknown)}"
        raise ProverToolError(msg)
    missing = sorted(_ACTIVE_KEYS - set(document))
    if missing:
        msg = f"snapshot active pointer is missing keys: {', '.join(missing)}"
        raise ProverToolError(msg)
    version = require_int(document["schema_version"], "active pointer schema_version")
    if version != SCHEMA_VERSION:
        msg = f"snapshot active pointer schema_version must be {SCHEMA_VERSION}"
        raise ProverToolError(msg)
    generation = require_truthy_text(
        document["generation"], "active pointer generation"
    )
    if not is_safe_generation_name(generation):
        msg = f"snapshot active pointer names an unsafe generation: {generation!r}"
        raise ProverToolError(msg)
    return ActivePointer(schema_version=version, generation=generation)


def write_active(layout: SnapshotLayout, generation: str) -> None:
    """Atomically replace the active pointer with one generation name.

    The pointer bytes are fsynced before the rename and the identity
    directory is fsynced afterwards, so a crash exposes either the old pointer
    or the new one, never a partial document.
    """
    if not is_safe_generation_name(generation):
        msg = f"snapshot generation name is not one safe component: {generation!r}"
        raise ProverToolError(msg)
    encoded = json.dumps(
        {"schema_version": SCHEMA_VERSION, "generation": generation},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    pending = layout.active_path.with_name(
        f".active-{os.getpid()}-{time.monotonic_ns()}"
    )
    try:
        descriptor = os.open(
            pending,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
            _PRIVATE_FILE_MODE,
        )
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        pending.replace(layout.active_path)
        fsync_directory(layout.identity_dir)
    except OSError as exc:
        pending.unlink(missing_ok=True)
        msg = f"failed to publish snapshot active pointer: {exc}"
        raise ProverToolError(msg) from exc


def fsync_directory(path: Path) -> None:
    """Flush a directory entry so the rename survives a crash."""
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@contextlib.contextmanager
def identity_lock(
    options: InstallOptions,
    *,
    timeout: float,
    clock: cabc.Callable[[], float] = time.monotonic,
) -> cabc.Generator[None]:
    """Hold the per-identity advisory lock, waiting at most `timeout`.

    The lock file is created private. A timeout is reported as a snapshot
    error rather than retried indefinitely, so a stuck owner cannot hang a
    proof forever.
    """
    identity_dir = identity_directory(options.pin, cache_home=options.cache_home)
    ensure_private_directory(identity_dir)
    path = identity_dir / "install.lock"
    try:
        descriptor = os.open(
            path,
            os.O_RDWR | os.O_CREAT | os.O_CLOEXEC,
            _PRIVATE_FILE_MODE,
        )
    except OSError as exc:
        msg = f"failed to open snapshot install lock '{path}': {exc}"
        raise ProverToolError(msg) from exc
    try:
        deadline = clock() + timeout
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if clock() >= deadline:
                    msg = "timed out waiting for the snapshot installation lock"
                    raise ProverToolError(msg) from None
                time.sleep(_LOCK_POLL_SECONDS)
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def ensure_private_directory(path: Path) -> None:
    """Create one cache directory with private permissions."""
    try:
        path.mkdir(mode=_PRIVATE_DIRECTORY_MODE, parents=True, exist_ok=True)
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
    _require_runtime_files(runtime)
    _check_toolchain_link(runtime, receipt.toolchain_link_target)
    validate_cached_payloads(layout, manifest)
    validate_installed_tree(layout, manifest, runtime)
    budget = None if remaining_budget is None else remaining_budget()
    evidence = probe(runtime, budget)
    _check_evidence(evidence, manifest, runtime)
    if evidence.observed != receipt.observed:
        msg = "snapshot receipt observation does not match the installed compiler"
        raise ProverToolError(msg)
    return build_identity_report(options.pin, manifest, runtime)


def _require_runtime_files(runtime: SnapshotRuntime) -> None:
    """Require every executable the runtime will invoke to be present."""
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
