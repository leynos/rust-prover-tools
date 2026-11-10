"""The completion watermark for one snapshot identity.

A publication marker is retired only by an installation that finished after
the marker was written, so the cache needs a durable record of the last
completed installation that is independent of the activation order. The
activation sequence cannot carry that meaning: an unrelated activation, such
as one whose worker was abandoned after its link, can outrank the record a
publication observed without that publication having completed.

The watermark is one small file per identity, written only by a lock-holding
installation after its activation record is confirmed durable. A publication
records the highest activation record it observes when it begins, and its
marker is pending until the watermark rises above that base. Because an
installation links its record before it records completion, a watermark above
a publication's base always names an installation that started after that
publication, while a completion write that lands late ranks no higher than a
base any later publication observed. An absent, unreadable or malformed
watermark reads as zero, which only ever keeps a marker pending.
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

    The value always comes from the activation order, and the record is linked
    before this write, so a value that lands late still ranks no higher than
    the base of any publication that began after the link. A marker is pending
    while the watermark has not risen above its base, so an abandoned write
    cannot retire a later unfinished publication; a write that never lands
    leaves the watermark low, which also keeps markers pending, and the next
    installation corrects it.
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
