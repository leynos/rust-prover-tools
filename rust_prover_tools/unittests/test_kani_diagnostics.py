"""Hold the JSONL boundary rule to its explicit, named preparation step."""

from __future__ import annotations

import contextlib
import typing as typ

from rust_prover_tools.kani_diagnostics import (
    open_destinations,
    open_diagnostics,
    prepare_jsonl_destination,
)
from rust_prover_tools.supervision import (
    DiagnosticSinks,
    JsonlSink,
    SupervisionConfig,
    jsonl_next_offset,
)
from rust_prover_tools.unittests.supervision_doubles import unwritten

if typ.TYPE_CHECKING:
    from pathlib import Path

#: A JSONL record whose last byte is not a line boundary.
_PARTIAL = b'{"record": "output"}'


def _partial(path: Path) -> Path:
    """Return a JSONL destination whose final record has no newline."""
    path.write_bytes(_PARTIAL)
    return path


def test_preparation_completes_a_partial_record(tmp_path: Path) -> None:
    """The explicit step appends the boundary byte and names the next offset."""
    path = _partial(tmp_path / "evidence.jsonl")

    offset = prepare_jsonl_destination(path)

    assert offset == len(_PARTIAL) + 1
    assert path.read_bytes() == _PARTIAL + b"\n"
    assert prepare_jsonl_destination(path) == offset
    assert path.read_bytes() == _PARTIAL + b"\n", "a prepared file was rewritten"


def test_opening_never_prepares_the_jsonl_boundary(tmp_path: Path) -> None:
    """A direct opener leaves a partial record exactly as it found it."""
    path = _partial(tmp_path / "evidence.jsonl")

    with contextlib.ExitStack() as stack:
        diagnostics = open_destinations(
            stack,
            DiagnosticSinks(jsonl_path=path),
            human_stream=None,
            max_line_bytes=1024,
        )

        assert isinstance(diagnostics.jsonl, JsonlSink)
        assert diagnostics.jsonl.offset == 0

    assert path.read_bytes() == _PARTIAL


def test_the_library_entry_point_prepares_before_opening(
    tmp_path: Path,
) -> None:
    """`open_diagnostics` performs the explicit preparation itself."""
    path = _partial(tmp_path / "evidence.jsonl")
    config = SupervisionConfig(sinks=DiagnosticSinks(jsonl_path=path))

    with open_diagnostics(config, None) as diagnostics:
        assert diagnostics.jsonl.offset == len(_PARTIAL) + 1

    assert path.read_bytes() == _PARTIAL + b"\n"


def test_the_offset_query_never_writes(tmp_path: Path) -> None:
    """A partial record and a missing destination are both left untouched."""
    path = _partial(tmp_path / "evidence.jsonl")
    missing = tmp_path / "missing.jsonl"

    assert jsonl_next_offset(path) == len(_PARTIAL) + 1
    assert path.read_bytes() == _PARTIAL
    assert jsonl_next_offset(missing) == 0
    assert unwritten(missing), "the query created the destination"
