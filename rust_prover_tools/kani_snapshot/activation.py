"""Durable, ordered activation records for the snapshot cache.

The active generation is the highest-numbered record in the identity's
`active` directory, never a single mutable file. A record is published by
hard-linking a fully written, fsynced document to the next free sequence
number, which makes activation a create-if-absent operation: two activations
cannot overwrite each other, and an activation that was abandoned and later
completes can only compete for the sequence number it observed. A later
activation has already taken that number or a higher one, so the abandoned
worker can neither replace the newer record nor outrank it. Readers resolve
the highest record, which is why supervision may abandon an activation without
letting it change what a later operation already published.

Durability of activation is part of the contract: a record that cannot be made
durable is reported as active but unconfirmed, and reuse reissues that flush
before reporting success. Once the record is confirmed durable, the activation
also advances the identity's completion watermark, which is what tells readers
that an installation finished rather than merely that a record exists. Because
the record is linked before that watermark is written, a completion that lands
late still ranks no higher than the base of a publication that began after the
link, so abandoning the write cannot retire a later unfinished marker.
"""

from __future__ import annotations

import dataclasses as dc
import functools
import json
import logging
import operator
import os
import secrets
import typing as typ

from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_snapshot.cleanup import discard_private_file
from rust_prover_tools.kani_snapshot.completion import record_completion
from rust_prover_tools.kani_snapshot.durability import confirm_activation
from rust_prover_tools.kani_snapshot.models import SCHEMA_VERSION
from rust_prover_tools.kani_snapshot.snapshot_layout import (
    SnapshotLayout,
    is_safe_generation_name,
)
from rust_prover_tools.kani_snapshot.strict_json import strict_json_loads
from rust_prover_tools.kani_snapshot.validation import (
    require_int,
    require_mapping,
    require_truthy_text,
)

if typ.TYPE_CHECKING:
    from pathlib import Path

    from rust_prover_tools.kani_snapshot.install_model import InstallBudget

__all__ = (
    "ActivePointer",
    "highest_sequence",
    "load_active",
    "write_active",
)

_LOGGER = logging.getLogger(__name__)
_PRIVATE_FILE_MODE: typ.Final[int] = 0o600
_ENTRY_SUFFIX: typ.Final[str] = ".json"
_ENTRY_DIGITS: typ.Final[int] = 20
_PENDING_PREFIX: typ.Final[str] = ".pending-"
_MAX_SEQUENCE_ATTEMPTS: typ.Final[int] = 8
_ACTIVE_KEYS: typ.Final[frozenset[str]] = frozenset({
    "schema_version",
    "generation",
})


@dc.dataclass(frozen=True, slots=True)
class ActivePointer:
    """The activation record that names the active generation.

    Attributes
    ----------
    schema_version : int
        Record schema version. Only version 1 is accepted.
    generation : str
        One safe generation directory name.
    """

    schema_version: int
    generation: str


def highest_sequence(layout: SnapshotLayout) -> int:
    """Return the highest published activation sequence, or zero.

    Only record names are read, so a damaged newest record still supplies
    its sequence. Publication needs the ordering rather than the pointer, and
    refusing to compute it would let a corrupt document block the repair that
    replaces it.
    """
    entries = _activation_entries(layout)
    return 0 if not entries else entries[-1][0]


def load_active(layout: SnapshotLayout) -> ActivePointer | None:
    """Read the highest activation record, or return `None` when none exists.

    Records are immutable once published, so resolving by sequence number is
    enough: no partial document can be observed, and a record added late by an
    abandoned activation ranks below any activation that followed it.
    """
    entries = _activation_entries(layout)
    if not entries:
        return None
    _, path = entries[-1]
    try:
        data = path.read_bytes()
    except OSError as exc:
        msg = f"failed to read snapshot activation record: {exc}"
        raise ProverToolError(msg) from exc
    return _parse_record(data, path.name)


def write_active(
    layout: SnapshotLayout,
    generation: str,
    *,
    budget: InstallBudget | None = None,
) -> None:
    """Record one generation as active under the next free sequence number.

    The record is written privately and fsynced, then hard-linked into place.
    A link that finds its target taken retries with the next number, so a
    concurrent or abandoned activation can never overwrite the record this
    call publishes. Publication is gated on the remaining budget immediately
    before the link, and the link itself is supervised.

    A link that outlives the budget is reported as an unknown outcome: the
    abandoned worker may still add its record, but only at the sequence number
    it observed, so it cannot displace a later activation. The directory
    flush that follows publication makes the record survive a crash; a flush
    that times out or fails is reported as an unconfirmed activation rather
    than as a publication failure.
    """
    if not is_safe_generation_name(generation):
        msg = f"snapshot generation name is not one safe component: {generation!r}"
        raise ProverToolError(msg)
    encoded = (
        json.dumps(
            {"schema_version": SCHEMA_VERSION, "generation": generation},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
        + b"\n"
    )
    _ensure_activations_dir(layout, budget)
    pending = (
        layout.activations_dir
        / f"{_PENDING_PREFIX}{os.getpid():x}-{secrets.token_hex(8)}"
    )
    try:
        _write_pending_record(pending, encoded, budget)
        if budget is not None:
            budget.check("pointer publication")
        sequence = _publish_record(layout, pending, budget)
    except ProverToolError:
        # Discarding the private document stops a worker that has not yet
        # issued its link; one that already has is protected by the
        # create-only rule instead. The removal is bounded and deferred when
        # the budget has expired, since a stalled unlink must not hold the
        # caller past the deadline.
        discard_private_file(pending, budget=budget, phase="activation record cleanup")
        raise
    except OSError as exc:
        discard_private_file(pending, budget=budget, phase="activation record cleanup")
        msg = f"failed to publish snapshot active pointer: {exc}"
        raise ProverToolError(msg) from exc
    discard_private_file(pending, budget=budget, phase="activation record cleanup")
    confirm_activation(layout, budget)
    record_completion(layout, sequence, budget=budget)
    _prune_superseded(layout, sequence, budget)


def _activation_entries(layout: SnapshotLayout) -> tuple[tuple[int, Path], ...]:
    """Return every activation record as `(sequence, path)`, oldest first."""
    try:
        children = list(layout.activations_dir.iterdir())
    except FileNotFoundError:
        return ()
    except OSError as exc:
        msg = f"failed to list snapshot activation records: {exc}"
        raise ProverToolError(msg) from exc
    entries: list[tuple[int, Path]] = []
    for path in children:
        sequence = _sequence_of(path.name)
        if sequence is not None:
            entries.append((sequence, path))
    entries.sort(key=operator.itemgetter(0))
    return tuple(entries)


def _sequence_of(name: str) -> int | None:
    """Return the sequence number of one canonical record name, if it is one."""
    if not name.endswith(_ENTRY_SUFFIX):
        return None
    stem = name.removesuffix(_ENTRY_SUFFIX)
    if len(stem) != _ENTRY_DIGITS or not (stem.isascii() and stem.isdigit()):
        return None
    return int(stem)


def _parse_record(data: bytes, name: str) -> ActivePointer:
    """Validate one activation record document."""
    document = require_mapping(
        strict_json_loads(data, "activation record"),
        "activation record document",
    )
    unknown = sorted(set(document) - _ACTIVE_KEYS)
    if unknown:
        msg = f"snapshot activation record has unknown keys: {', '.join(unknown)}"
        raise ProverToolError(msg)
    missing = sorted(_ACTIVE_KEYS - set(document))
    if missing:
        msg = f"snapshot activation record {name} is missing keys: {', '.join(missing)}"
        raise ProverToolError(msg)
    version = require_int(
        document["schema_version"],
        "activation record schema_version",
    )
    if version != SCHEMA_VERSION:
        msg = f"snapshot activation record schema_version must be {SCHEMA_VERSION}"
        raise ProverToolError(msg)
    generation = require_truthy_text(
        document["generation"], "activation record generation"
    )
    if not is_safe_generation_name(generation):
        msg = f"snapshot activation record names an unsafe generation: {generation!r}"
        raise ProverToolError(msg)
    return ActivePointer(schema_version=version, generation=generation)


def _ensure_activations_dir(
    layout: SnapshotLayout,
    budget: InstallBudget | None,
) -> None:
    """Create the record directory when it does not exist yet."""
    directory = layout.activations_dir

    def exists() -> bool:
        """Report whether the private record directory is present."""
        return directory.is_dir()

    try:
        present = (
            exists()
            if budget is None
            else budget.supervise("activation directory check", exists)
        )
    except OSError as exc:
        msg = f"failed to read snapshot activation directory: {exc}"
        raise ProverToolError(msg) from exc
    if present:
        return

    def create() -> None:
        """Create the private record directory."""
        directory.mkdir(mode=0o700, parents=False)

    try:
        if budget is None:
            create()
        else:
            budget.supervise("activation directory creation", create)
    except OSError as exc:
        msg = f"failed to create snapshot activation directory '{directory}': {exc}"
        raise ProverToolError(msg) from exc


def _prune_superseded(
    layout: SnapshotLayout,
    keep: int,
    budget: InstallBudget | None,
) -> None:
    """Remove records below `keep`, after the new record is durable.

    Pruning only ever removes records that rank below the newest one, so a
    worker that resumes late can delete stale evidence but can never remove
    the record readers resolve. A stall is deferred rather than reported: the
    activation is already durable and the leftover record is harmless.
    """

    def prune() -> None:
        """Remove every record older than the one just published."""
        for sequence, path in _activation_entries(layout):
            if sequence < keep:
                discard_private_file(path, phase="activation record cleanup")

    try:
        if budget is None:
            prune()
        else:
            budget.supervise("activation record cleanup", prune)
    except ProverToolError as exc:
        _LOGGER.debug("snapshot record cleanup deferred: %s", exc)


def _write_pending_record(
    pending: Path,
    encoded: bytes,
    budget: InstallBudget | None,
) -> None:
    """Write and fsync one private record, bounded by `budget`."""

    def write() -> None:
        """Create the private record and make its bytes durable."""
        descriptor = os.open(
            pending,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
            _PRIVATE_FILE_MODE,
        )
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())

    if budget is None:
        write()
    else:
        budget.supervise("pointer write", write)


def _publish_record(
    layout: SnapshotLayout,
    pending: Path,
    budget: InstallBudget | None,
) -> int:
    """Link the pending record at the next free sequence number.

    A link is create-if-absent, so losing a race is an ordinary retry rather
    than an overwrite. That is what makes an abandoned activation harmless:
    its sequence number was observed before the winner took it, so it can only
    fail or add a lower-ranked record.
    """
    for _ in range(_MAX_SEQUENCE_ATTEMPTS):
        sequence = _next_sequence(layout, budget)
        target = layout.activations_dir / f"{sequence:0{_ENTRY_DIGITS}d}{_ENTRY_SUFFIX}"
        try:
            _link_record(pending, target, budget)
        except FileExistsError:
            continue
        except ProverToolError as exc:
            msg = (
                "snapshot activation outcome is unknown: the deadline expired "
                "while recording the active generation, which a later check "
                "reports"
            )
            raise ProverToolError(msg) from exc
        return sequence
    msg = "could not allocate a snapshot activation sequence under contention"
    raise ProverToolError(msg)


def _next_sequence(layout: SnapshotLayout, budget: InstallBudget | None) -> int:
    """Return one more than the highest published sequence number.

    The listing is a filesystem read performed while the identity lock is
    held, so it is supervised like the link it precedes: a stalled directory
    read must not hold the caller past the deadline. Abandoning the read is
    safe because it observes the directory without changing it.
    """
    if budget is None:
        entries = _activation_entries(layout)
    else:
        entries = budget.supervise(
            "activation record enumeration",
            functools.partial(_activation_entries, layout),
        )
    return 1 if not entries else entries[-1][0] + 1


def _link_record(
    pending: Path,
    target: Path,
    budget: InstallBudget | None,
) -> None:
    """Hard-link the pending record to its sequence-numbered name.

    A budget supervises the link so a stalled filesystem cannot hold the
    caller past the deadline. The link itself is atomic and create-only, so an
    abandoned worker can add a record but never replace one.
    """
    link = functools.partial(os.link, pending, target)
    if budget is None:
        link()
    else:
        budget.supervise("pointer publication", link)
