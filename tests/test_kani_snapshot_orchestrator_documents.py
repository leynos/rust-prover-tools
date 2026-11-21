"""Rendered documents are written atomically and fail as this command.

The document is read by the next workflow job, so a reader must never observe a
half-written file: the bytes go to a sibling temporary file that is fsynced and
then renamed over the destination. Every filesystem step that can fail —
creating the directory, creating the temporary file, writing or renaming —
belongs to the same error boundary, so an unwritable destination is reported as
a `ProverToolError` naming the path rather than escaping as a bare `OSError`.
"""

from __future__ import annotations

import typing as typ

import pytest

from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_snapshot.orchestrator.documents import write_document

if typ.TYPE_CHECKING:
    import pathlib


def test_a_document_is_written_without_leaving_a_temporary_file(
    tmp_path: pathlib.Path,
) -> None:
    """The destination holds exactly the bytes and nothing else remains.

    The rename is what makes the write atomic, and it must also mean the
    temporary file a reader could otherwise see is gone.
    """
    destination = tmp_path / "nested" / "inventory.json"

    write_document(destination, b'{"assets": []}\n')

    assert destination.read_bytes() == b'{"assets": []}\n'
    assert sorted(path.name for path in destination.parent.iterdir()) == [
        "inventory.json"
    ], "a temporary file survived the write"


def test_a_destination_that_cannot_be_prepared_is_reported_not_raised_raw(
    tmp_path: pathlib.Path,
) -> None:
    """Directory creation is inside the error boundary.

    A destination below an existing regular file cannot be created at all.
    That failure happened before any document bytes were written, but it is
    still this command's failure to report: letting `FileExistsError` escape
    would present an unwritable output as an unhandled crash.
    """
    blocker = tmp_path / "payloads"
    blocker.write_text("not a directory", encoding="utf-8")

    with pytest.raises(ProverToolError, match="failed to write"):
        write_document(blocker / "inventory.json", b"{}")

    assert blocker.read_text(encoding="utf-8") == "not a directory"
