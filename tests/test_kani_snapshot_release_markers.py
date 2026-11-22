"""The markers a release body carries are parsed, never assumed.

A draft may be resumed only by the run and attempt that created it, so the run
marker is a trust boundary: `None` means "cannot prove", and must never be read
as attempt zero. These tests pin both directions of that boundary — a body the
writer produced parses back, and a body carrying something else is refused.

The digit cap is part of the refusal. An unbounded pattern would hand an
arbitrary number of digits to `int()`, which raises once the interpreter's
integer-string conversion limit is passed; a parser that can be made to crash by
release-body text is not a refusal.
"""

from __future__ import annotations

import pytest

from rust_prover_tools.kani_snapshot.release_markers import (
    parse_input_marker,
    parse_run_marker,
    release_body,
)

#: A fingerprint the input marker accepts: 64 lowercase hexadecimal digits.
_FINGERPRINT = "a" * 64


def test_a_written_body_parses_back_to_its_origin() -> None:
    """The writer and both parsers agree on one body."""
    body = release_body("kani-snapshot-1", _FINGERPRINT, run_id=12345, run_attempt=2)

    assert parse_run_marker(body) == (12345, 2)
    assert parse_input_marker(body) == _FINGERPRINT


@pytest.mark.parametrize("body", [None, "", "no markers here"])
def test_a_body_without_markers_proves_nothing(body: str | None) -> None:
    """Absence is reported as `None`, not as a default attempt."""
    assert parse_run_marker(body) is None
    assert parse_input_marker(body) is None


def test_a_twenty_digit_run_identifier_is_accepted() -> None:
    """A 64-bit identifier fits the cap, so the longest legal marker parses."""
    run_id = int("9" * 20)

    parsed = parse_run_marker(f"<!-- kani-snapshot-run: {run_id}/1 -->")

    assert parsed == (run_id, 1)


@pytest.mark.parametrize(
    "marker",
    [
        pytest.param("1" + "0" * 20, id="twenty-one-digits"),
        pytest.param("9" * 5000, id="far-past-int-conversion-limit"),
    ],
)
def test_an_overlong_run_identifier_is_refused_without_raising(marker: str) -> None:
    """A marker longer than the cap proves nothing; it is not an exception.

    `int()` refuses a digit string longer than the interpreter's conversion
    limit, so an unbounded pattern would turn hostile or corrupt release text
    into a crash instead of "cannot prove".
    """
    assert parse_run_marker(f"<!-- kani-snapshot-run: {marker}/1 -->") is None


def test_a_zero_run_identifier_is_refused() -> None:
    """Workflow identifiers start at one, so zero is not a run marker."""
    assert parse_run_marker("<!-- kani-snapshot-run: 0/1 -->") is None


def test_a_zero_attempt_is_refused() -> None:
    """An attempt number starts at one; zero would describe nothing."""
    assert parse_run_marker("<!-- kani-snapshot-run: 1/0 -->") is None


@pytest.mark.parametrize(
    "fingerprint",
    [
        pytest.param("a" * 63, id="too-short"),
        pytest.param("a" * 65, id="too-long"),
        pytest.param("A" * 64, id="uppercase"),
    ],
)
def test_a_fingerprint_that_is_not_a_sha256_is_refused(fingerprint: str) -> None:
    """Only a complete lowercase digest binds a release to build inputs."""
    body = f"<!-- kani-snapshot-input: {fingerprint} -->"

    assert parse_input_marker(body) is None
