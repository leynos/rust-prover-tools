"""An in-memory JSONL sink for tests and programmatic consumers.

It renders exactly the JSONL the file sink writes, so a test can assert the
shipped record shape while an embedder can inspect records without a file.
"""

from __future__ import annotations

import dataclasses as dc
import json
import typing as typ

from rust_prover_tools.supervision.formatting import (
    JSONL_SCHEMA_VERSION,
    fit_record,
    normalise,
)

__all__ = ("MemorySink",)


@dc.dataclass(slots=True)
class MemorySink:
    """An in-memory JSONL sink, for tests and programmatic consumers.

    Attributes
    ----------
    records : list[dict[str, object]]
        Every record written, in order.
    """

    records: list[dict[str, object]] = dc.field(default_factory=list)
    max_line_bytes: int = 8 * 1024
    _fragments: list[str] = dc.field(default_factory=list)
    _offset: int = 0

    @property
    def text(self) -> str:
        """The JSONL text written so far."""
        return "".join(self._fragments)

    def write(self, record: dict[str, object]) -> None:
        """Record one payload and its JSONL rendering."""
        payload = {"schema": JSONL_SCHEMA_VERSION, **record}
        normalised = typ.cast("dict[str, object]", normalise(payload))
        line = fit_record(normalised, self.max_line_bytes)
        self.records.append(typ.cast("dict[str, object]", json.loads(line)))
        self._fragments.append(line + "\n")
        self._offset += len((line + "\n").encode("utf-8"))

    def flush(self) -> None:
        """Do nothing: a memory sink is never buffered."""

    @property
    def offset(self) -> int:
        """The number of bytes rendered so far."""
        return self._offset
