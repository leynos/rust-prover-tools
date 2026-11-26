"""Publish a rendered report to its destination.

Rendering is pure and lives in ``report``; this module is the only place the
supervisor writes a report to the filesystem. Keeping the write separate is
what lets a caller inject a recording writer, or a failing one, without
touching the render contract.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
import typing as typ
from pathlib import Path

from rust_prover_tools.supervision.report import report_document

if typ.TYPE_CHECKING:
    from rust_prover_tools.supervision.results import RunReport

__all__ = ("write_report",)


def write_report(path: Path, report: RunReport) -> None:
    """Write the JSON report atomically.

    Parameters
    ----------
    path : Path
        Destination path.
    report : RunReport
        Report to write.

    Notes
    -----
    The document is written to a sibling temporary file, flushed, synced, and
    renamed over the destination. An interrupted write therefore leaves the
    previous report intact rather than a half-written one.
    """
    destination = path.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=destination.parent,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(
                report_document(report),
                handle,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        Path(temporary).replace(destination)
    except BaseException:
        with contextlib.suppress(OSError):
            Path(temporary).unlink()
        raise
