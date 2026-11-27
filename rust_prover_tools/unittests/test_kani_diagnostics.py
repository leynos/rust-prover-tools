"""Hold the JSONL boundary rule to its explicit, named preparation step."""

from __future__ import annotations

import contextlib
from pathlib import Path  # ruff: ignore[typing-only-standard-library-import]

import pytest

from rust_prover_tools.kani_diagnostics import (
    open_destinations,
    prepare_and_open_diagnostics,
    prepare_jsonl_destination,
)
from rust_prover_tools.supervision import (
    Diagnostics,
    DiagnosticSinks,
    JsonlSink,
    MemorySink,
    SupervisionConfig,
    jsonl_next_offset,
)
from rust_prover_tools.unittests.supervision_doubles import unwritten

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


def test_the_opener_names_the_preparation_it_performs(tmp_path: Path) -> None:
    """The named opener repairs once, and its sinks start at that offset.

    The repaired offset is where the first record lands, so a run that follows
    an unterminated record writes into a file with exactly one new boundary
    byte and no duplicated record.
    """
    path = _partial(tmp_path / "evidence.jsonl")
    config = SupervisionConfig(sinks=DiagnosticSinks(jsonl_path=path))
    repaired = len(_PARTIAL) + 1

    with prepare_and_open_diagnostics(config, None) as diagnostics:
        assert isinstance(diagnostics.jsonl, JsonlSink)
        assert diagnostics.jsonl.offset == repaired

    assert path.read_bytes() == _PARTIAL + b"\n", "the boundary was written twice"
    assert jsonl_next_offset(path) == repaired


def test_a_provided_diagnostics_skips_preparation(tmp_path: Path) -> None:
    """A caller-owned object is yielded unchanged and nothing is written."""
    from rust_prover_tools.supervision import Diagnostics, MemorySink

    path = _partial(tmp_path / "evidence.jsonl")
    config = SupervisionConfig(sinks=DiagnosticSinks(jsonl_path=path))
    provided = Diagnostics(jsonl=MemorySink())

    with prepare_and_open_diagnostics(config, provided) as diagnostics:
        assert diagnostics is provided

    assert path.read_bytes() == _PARTIAL


def test_the_offset_query_never_writes(tmp_path: Path) -> None:
    """A partial record and a missing destination are both left untouched."""
    path = _partial(tmp_path / "evidence.jsonl")
    missing = tmp_path / "missing.jsonl"

    assert jsonl_next_offset(path) == len(_PARTIAL) + 1
    assert path.read_bytes() == _PARTIAL
    assert jsonl_next_offset(missing) == 0
    assert unwritten(missing), "the query created the destination"


def test_a_jsonl_destination_may_not_alias_the_human_stream(
    tmp_path: Path,
) -> None:
    """The effective human stream and the JSONL file cannot be one handle."""
    path = tmp_path / "evidence.jsonl"
    path.touch()
    config = SupervisionConfig(sinks=DiagnosticSinks(jsonl_path=path))

    with (
        path.open("a", encoding="utf-8") as handle,
        pytest.raises(ValueError, match="human stream"),
        prepare_and_open_diagnostics(config, None, human_stream=handle),
    ):
        pass


def test_a_report_destination_may_not_alias_the_human_stream(
    tmp_path: Path,
) -> None:
    """The effective human stream and the report file cannot be one handle."""
    path = tmp_path / "report.json"
    path.touch()
    config = SupervisionConfig(sinks=DiagnosticSinks(report_path=path))

    with (
        path.open("a", encoding="utf-8") as handle,
        pytest.raises(ValueError, match="report destination"),
        prepare_and_open_diagnostics(config, None, human_stream=handle),
    ):
        pass


def test_a_configured_human_path_replaces_the_fallback_stream(
    tmp_path: Path,
) -> None:
    """A human path wins, so an aliased fallback stream is never used."""
    path = tmp_path / "report.json"
    path.touch()
    config = SupervisionConfig(
        sinks=DiagnosticSinks(
            human_path=tmp_path / "human.log",
            report_path=path,
        )
    )

    with (
        path.open("a", encoding="utf-8") as handle,
        prepare_and_open_diagnostics(config, None, human_stream=handle) as opened,
    ):
        assert opened.human is not None


def test_a_provided_diagnostics_still_validates_the_configured_sinks(
    tmp_path: Path,
) -> None:
    """An override cannot hide two configured roles naming one file."""
    shared = tmp_path / "evidence.jsonl"
    shared.touch()
    config = SupervisionConfig(
        sinks=DiagnosticSinks(jsonl_path=shared, report_path=shared)
    )
    provided = Diagnostics(jsonl=MemorySink())

    with (
        pytest.raises(ValueError, match="distinct"),
        prepare_and_open_diagnostics(config, provided),
    ):
        pass
