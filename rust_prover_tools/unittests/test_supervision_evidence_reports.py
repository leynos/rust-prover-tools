"""Hold report rendering, publication, and destinations to their contract."""

from __future__ import annotations

import dataclasses as dc
import json
import typing as typ
from pathlib import Path

import pytest

from rust_prover_tools.supervision import (
    REPORT_NOTES,
    DiagnosticSinks,
    EvidenceFormatError,
    SupervisionConfig,
    render_table,
    report_document,
    validate_report_document,
    write_report,
)
from rust_prover_tools.unittests.supervision_evidence_support import (
    _record,
    _report,
)


def test_the_report_document_carries_the_schema_and_notes() -> None:
    """The final report is versioned and states its measurement limits."""
    document = report_document(_report())

    assert document["schema"] == 1
    assert document["passed"] is True
    assert document["notes"] == list(REPORT_NOTES)
    validate_report_document(document)


def test_an_unknown_report_key_is_refused() -> None:
    """A schema that is not enforced drifts."""
    document = report_document(_report())
    document["surprise"] = 1

    with pytest.raises(EvidenceFormatError, match="unknown"):
        validate_report_document(document)


def test_a_missing_report_key_is_refused() -> None:
    """A report that omits a documented field is refused."""
    document = report_document(_report())
    del document["outcome"]

    with pytest.raises(EvidenceFormatError, match="missing"):
        validate_report_document(document)


def test_a_measurement_that_is_neither_form_is_refused() -> None:
    """An empty measurement object is not evidence."""
    document = report_document(_report())
    results = typ.cast("list[dict[str, object]]", document["results"])
    results[0]["peak_group_rss"] = {}

    with pytest.raises(EvidenceFormatError, match="neither"):
        validate_report_document(document)


def test_a_report_is_written_atomically(tmp_path: Path) -> None:
    """The destination holds a complete JSON document after the call."""
    destination = tmp_path / "report.json"

    write_report(destination, _report())

    document = json.loads(destination.read_text(encoding="utf-8"))
    validate_report_document(document)
    assert not list(tmp_path.glob("*.tmp"))


def test_the_table_names_every_result_and_the_limits() -> None:
    """The human summary carries the identity, status, and measurement notes."""
    lines = render_table(_report())

    assert "PACKAGE" in lines[0]
    assert "HARNESS" in lines[0]
    assert any("harnesses::verified" in line for line in lines)
    assert any(line.startswith("note:") for line in lines)
    assert any("outcome=passed" in line for line in lines)


def test_the_named_opener_starts_at_a_line_boundary(tmp_path: Path) -> None:
    """A file without a final newline gains one before the next record."""
    from rust_prover_tools.kani_diagnostics import prepare_and_open_diagnostics

    path = tmp_path / "evidence.jsonl"
    path.write_text("x" * 100, encoding="utf-8")
    config = SupervisionConfig(sinks=DiagnosticSinks(jsonl_path=path))

    with prepare_and_open_diagnostics(config, None) as diagnostics:
        reference = diagnostics.emit(_record())

    assert reference is not None
    assert reference.offset == 101
    assert path.read_text(encoding="utf-8").splitlines()[0] == "x" * 100


def test_a_symlink_alias_is_refused_before_a_handle_is_opened(
    tmp_path: Path,
) -> None:
    """Resolved paths, not spellings, decide whether two roles collide.

    A library caller reaches this rule through the adapter that opens the
    destinations, which must refuse the alias before appending anything.
    """
    from rust_prover_tools.kani_diagnostics import prepare_and_open_diagnostics

    target = tmp_path / "evidence"
    target.touch()
    alias = tmp_path / "alias"
    alias.symlink_to(target)
    config = SupervisionConfig(
        sinks=DiagnosticSinks(jsonl_path=target, report_path=alias)
    )

    with (
        pytest.raises(ValueError, match="distinct"),
        prepare_and_open_diagnostics(config, None),
    ):
        pass
    assert target.read_text(encoding="utf-8") == ""


def test_a_hard_link_alias_is_refused_before_a_handle_is_opened(
    tmp_path: Path,
) -> None:
    """Resolved paths differ, but one inode under two names is still shared."""
    from rust_prover_tools.kani_diagnostics import prepare_and_open_diagnostics

    first = tmp_path / "first.log"
    first.touch()
    second = tmp_path / "second.log"
    second.hardlink_to(first)
    config = SupervisionConfig(
        sinks=DiagnosticSinks(human_path=first, jsonl_path=second)
    )

    with (
        pytest.raises(ValueError, match="distinct"),
        prepare_and_open_diagnostics(config, None),
    ):
        pass
    assert first.read_text(encoding="utf-8") == ""


def test_an_unstatable_destination_is_not_reported_as_an_alias(
    tmp_path: Path,
) -> None:
    """A stat failure is not a collision; the open that follows reports it."""
    from rust_prover_tools.kani_diagnostics import prepare_and_open_diagnostics
    from rust_prover_tools.supervision.destinations import (
        require_distinct_destinations,
    )

    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    broken = blocker / "evidence.jsonl"
    report = tmp_path / "report.json"

    # The identity check cannot stat through a file, so it reports no alias.
    require_distinct_destinations(jsonl_path=broken, report_path=report)

    config = SupervisionConfig(sinks=DiagnosticSinks(jsonl_path=broken))
    with (
        pytest.raises(OSError, match="File exists"),
        prepare_and_open_diagnostics(config, None),
    ):
        pass


def test_a_failed_report_publication_keeps_the_previous_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The atomic rename cannot destroy a good report when it fails."""
    path = tmp_path / "report.json"
    path.write_text("previous report", encoding="utf-8")

    def fail(self: Path, target: Path) -> None:
        """Refuse the publication rename."""
        del self, target
        msg = "rename failed"
        raise OSError(msg)

    monkeypatch.setattr(Path, "replace", fail)

    with pytest.raises(OSError, match="rename failed"):
        write_report(path, _report())

    assert path.read_text(encoding="utf-8") == "previous report"
    assert not list(tmp_path.glob(".report.json.*.tmp"))


def test_a_multiline_detail_stays_on_one_table_row() -> None:
    """A detail cannot split its row into unrelated log lines."""
    report = _report()
    result = report.results[0]
    report = dc.replace(
        report,
        results=(dc.replace(result, detail="first\nsecond"),),
    )

    lines = render_table(report)

    assert all("\n" not in line for line in lines)
    assert any("first second" in line for line in lines)
