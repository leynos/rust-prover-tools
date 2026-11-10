"""The completion watermark for one snapshot identity.

A publication marker is retired only by an installation that finished after
the marker was written, so the cache needs a durable record of the last
completed installation that is independent of the activation order. The
activation sequence cannot carry that meaning: an unrelated activation, such
as one whose worker was abandoned after its link, can outrank the record a
publication observed without that publication having completed.

The watermark is one small file per identity, written only by a lock-holding
installation after its activation record is confirmed durable. A publication
reads it once, before writing its marker, and records the value it observed.
A marker is pending unless the watermark has since advanced past it, so an
absent, unreadable or malformed watermark only ever keeps a marker pending.
"""

from __future__ import annotations

import typing as typ

from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_snapshot.install_generation import write_generation_file

if typ.TYPE_CHECKING:
    from rust_prover_tools.kani_snapshot.install_model import InstallBudget
    from rust_prover_tools.kani_snapshot.snapshot_layout import SnapshotLayout

__all__ = ("completion_watermark", "record_completion")

_FILE_NAME: typ.Final[str] = "completed"


def completion_watermark(layout: SnapshotLayout) -> int:
    """Return the sequence of the last completed installation, or zero.

    The file is written only after a lock-holding installation confirmed its
    activation record durable, so its value always names an installation that
    finished. A missing, unreadable or malformed file names no completed
    installation, and reporting zero for it only keeps markers pending, which
    makes readers refuse the cache until an installation completes.
    """
    path = layout.identity_dir / _FILE_NAME
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return 0
    except OSError as exc:
        msg = f"failed to read snapshot completion watermark: {exc}"
        raise ProverToolError(msg) from exc
    text = data.strip()
    if not (text.isascii() and text.isdigit()):
        return 0
    return int(text)


def record_completion(
    layout: SnapshotLayout,
    sequence: int,
    *,
    budget: InstallBudget | None = None,
) -> None:
    """Record `sequence` as the last completed installation.

    The value always comes from the activation order, so a write abandoned at
    the deadline can at worst publish a sequence that a later installation has
    already passed. Lowering the watermark cannot retire a marker, because a
    marker is pending while its own completion is not older than the
    watermark, so that outcome is safe and the next installation corrects it.
    """
    if sequence <= 0:
        msg = "snapshot completion sequence must be positive"
        raise ProverToolError(msg)
    write_generation_file(
        layout.identity_dir / _FILE_NAME,
        f"{sequence}\n".encode("ascii"),
        "completion watermark",
        budget=budget,
    )
