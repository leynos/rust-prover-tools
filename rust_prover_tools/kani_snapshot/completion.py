"""The completion evidence for one snapshot identity.

A publication marker is retired only by an installation that finished after
the marker was written, so the cache needs a durable record of completed
installations that is independent of the activation order. The activation
sequence cannot carry that meaning: an unrelated activation, such as one whose
worker was abandoned after its link, can outrank the record a publication
observed without that publication having completed.

The evidence is one create-only record per completed installation, named by the
sequence it completed and written only by a lock-holding installation after its
activation record is confirmed durable. A publication records the highest
activation record it observes when it begins, and its marker is pending until
the watermark rises above that base. Because an installation links its record
before it records completion, a watermark above a publication's base always
names an installation that started after that publication, while a late
completion adds a record beneath any newer one: it cannot retire a later
unfinished marker, and it cannot regress the evidence a later installation
already recorded. Only evidence this module writes counts: a record must have
the canonical name, be a regular file, and name the same sequence in its
contents. An absent, unreadable, mismatched or malformed record is ignored,
and a watermark with no records reads as zero, which only ever keeps a marker
pending.
"""

from __future__ import annotations

import logging
import typing as typ

from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_snapshot.cleanup import discard_private_file
from rust_prover_tools.kani_snapshot.install_generation import write_generation_file

if typ.TYPE_CHECKING:
    from pathlib import Path

    from rust_prover_tools.kani_snapshot.install_model import InstallBudget
    from rust_prover_tools.kani_snapshot.snapshot_layout import SnapshotLayout

__all__ = ("completion_watermark", "record_completion")

_LOGGER = logging.getLogger(__name__)

#: Prefix of a create-only record naming one completed installation.
_RECORD_PREFIX: typ.Final[str] = "completed."
_RECORD_DIGITS: typ.Final[int] = 20


def completion_watermark(layout: SnapshotLayout) -> int:
    """Return the highest completion sequence recorded, or zero.

    Each completed installation owns a record named by its sequence, so a
    write that lands after a newer one adds a record this answer ignores
    rather than lowering it. A missing directory, a name that is not
    canonical, an entry that is not a regular file, and unreadable or
    mismatched contents all name no completed installation; reporting zero
    for those only keeps markers pending, which makes readers refuse the cache
    until an installation completes.
    """
    sequences = (
        sequence
        for path in _identity_entries(layout)
        if (sequence := _record_sequence(path)) is not None
    )
    return max(sequences, default=0)


def record_completion(
    layout: SnapshotLayout,
    sequence: int,
    *,
    budget: InstallBudget | None = None,
) -> None:
    """Record `sequence` as one completed installation.

    The record is named by its sequence and created once, so a write abandoned
    at the deadline and landed after a newer installation cannot replace that
    installation's record; it joins the order beneath it, where the watermark
    ignores it. The activation record is linked before this write, so the
    sequence still ranks no higher than the base of any publication that began
    after that link: it cannot retire a later unfinished marker, while a
    completion above a base always names an installation that started after
    the publication. Older records are pruned after the new one is durable,
    and a deferred prune is only a harmless leftover.
    """
    if sequence <= 0:
        msg = "snapshot completion sequence must be positive"
        raise ProverToolError(msg)
    write_generation_file(
        _record_path(layout, sequence),
        f"{sequence}\n".encode("ascii"),
        "completion watermark",
        budget=budget,
    )
    _prune_superseded(layout, budget)


def _record_path(layout: SnapshotLayout, sequence: int) -> Path:
    """Return the create-only record naming one completed installation."""
    return layout.identity_dir / f"{_RECORD_PREFIX}{sequence:0{_RECORD_DIGITS}d}"


def _record_sequence(path: Path) -> int | None:
    """Return the sequence one completion record names, if it is canonical.

    Completion evidence retires publication markers, so only a record this
    module writes may count: a name of exactly the documented width, a regular
    file rather than a directory or a symlink, and content naming the same
    sequence. Anything else names no completed installation and is ignored,
    which keeps markers pending and makes readers refuse the cache rather than
    trust state the installer cannot account for.
    """
    name = path.name
    if not name.startswith(_RECORD_PREFIX):
        return None
    digits = name.removeprefix(_RECORD_PREFIX)
    if len(digits) != _RECORD_DIGITS or not (digits.isascii() and digits.isdigit()):
        return None
    sequence = int(digits)
    if sequence <= 0:
        return None
    return sequence if _record_names(path, sequence) else None


def _record_names(path: Path, sequence: int) -> bool:
    """Report whether `path` is a regular file whose bytes name `sequence`."""
    expected = f"{sequence}\n".encode("ascii")
    try:
        if not path.is_file(follow_symlinks=False):
            return False
        return path.read_bytes() == expected
    except OSError:
        return False


def _identity_entries(layout: SnapshotLayout) -> list[Path]:
    """Return every identity entry, or none when the directory is absent."""
    try:
        return list(layout.identity_dir.iterdir())
    except FileNotFoundError:
        return []
    except OSError as exc:
        msg = f"failed to read snapshot completion watermark: {exc}"
        raise ProverToolError(msg) from exc


def _prune_superseded(
    layout: SnapshotLayout,
    budget: InstallBudget | None,
) -> None:
    """Remove completion records ranking below the highest recorded one.

    The highest value is recomputed rather than taken from the sequence just
    written, so a worker that resumes after a newer installation completed
    removes only records beneath that newer value and never the record readers
    resolve. A stall is deferred: the completion is already durable and a
    leftover record is ignored by the watermark.
    """

    def prune() -> None:
        """Remove every record below the highest completion recorded."""
        highest = completion_watermark(layout)
        for path in _identity_entries(layout):
            sequence = _record_sequence(path)
            if sequence is not None and sequence < highest:
                discard_private_file(path, phase="completion record cleanup")

    try:
        if budget is None:
            prune()
        else:
            budget.supervise("completion record cleanup", prune)
    except ProverToolError as exc:
        _LOGGER.debug("snapshot completion record cleanup deferred: %s", exc)
