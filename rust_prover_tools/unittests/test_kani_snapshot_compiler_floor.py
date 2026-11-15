"""Tests for the demonstrated Rust compiler floor.

The floor claim has two halves that must be checked separately: the pinned
compiler accepts the 1.94 `array_windows` API and proves the harness, and a
1.93 compiler rejects the same code as unstable. The transcripts here are the
exact output EP-M1 captured, so the claim cannot drift away from its evidence.
"""

from __future__ import annotations

import pathlib
import typing as typ

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_snapshot.producer.compiler_floor import (
    COMPILER_FLOOR,
    FLOOR_HARNESS,
    parse_rustc_version,
    require_compiler_floor,
    require_floor_harness_verified,
    require_unstable_feature_rejected,
)

FIXTURES: typ.Final[pathlib.Path] = (
    pathlib.Path(__file__).resolve().parents[2]
    / "tests"
    / "fixtures"
    / "kani_snapshot"
    / "compiler_floor"
)


def _fixture(name: str) -> str:
    """Return one captured transcript or source file as text."""
    return (FIXTURES / name).read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("rustc 1.100.0-nightly (8925ea358 2026-08-20)", (1, 100)),
        ("rustc 1.94.0 (4a4ef493e 2026-03-02)", (1, 94)),
        ("rustc 1.93.1 (01f6ddf75 2026-02-11)", (1, 93)),
    ],
)
def test_rustc_version_parses_the_release(line: str, expected: tuple[int, int]) -> None:
    """Both stable and nightly version lines yield the release numbers."""
    assert parse_rustc_version(line) == expected


@pytest.mark.parametrize("line", ["", "nightly-2026-08-21", "cargo 1.94.0", "rustc 1"])
def test_rustc_version_rejects_a_non_version_line(line: str) -> None:
    """A toolchain name or another tool's version is not a compiler version."""
    with pytest.raises(ProverToolError, match="rustc version"):
        parse_rustc_version(line)


@given(
    major=st.integers(min_value=1, max_value=9),
    minor=st.integers(min_value=0, max_value=200),
)
@settings(max_examples=50, deadline=None)
def test_floor_acceptance_tracks_the_announced_release(major: int, minor: int) -> None:
    """Acceptance is exactly the tuple comparison against the floor."""
    line = f"rustc {major}.{minor}.0 (012345678 2026-01-01)"
    meets = (major, minor) >= COMPILER_FLOOR
    if meets:
        require_compiler_floor(line)
    else:
        with pytest.raises(ProverToolError, match="below the snapshot floor"):
            require_compiler_floor(line)


def test_positive_transcript_verifies_the_floor_harness() -> None:
    """The captured run checked the named harness and verified it."""
    require_floor_harness_verified(_fixture("positive.txt"), FLOOR_HARNESS)


def test_positive_transcript_without_the_harness_is_refused() -> None:
    """A summary-only transcript cannot show which harness ran."""
    summary = "\n".join(
        line
        for line in _fixture("positive.txt").splitlines()
        if "Checking harness" not in line
    )
    with pytest.raises(ProverToolError, match="does not check harness"):
        require_floor_harness_verified(summary)


def test_negative_transcript_rejects_the_unstable_feature() -> None:
    """The 1.93 control failed because `array_windows` is unstable there."""
    require_unstable_feature_rejected(_fixture("negative_1_93.txt"))


def test_negative_transcript_for_another_reason_is_refused() -> None:
    """A syntax error would not demonstrate the missing stabilisation."""
    with pytest.raises(ProverToolError, match="E0658"):
        require_unstable_feature_rejected("error: expected one of `!` or `::`")


def test_positive_fixture_depends_on_the_floor_feature() -> None:
    """The harness source uses the API whose stabilisation defines the floor."""
    source = _fixture("positive.rs")
    assert "array_windows::<2>()" in source
    assert f"fn {FLOOR_HARNESS}()" in source
    assert "#[kani::proof]" in source


def test_negative_fixture_is_the_same_code_without_kani() -> None:
    """The control differs only by the absence of verification annotations."""
    negative = _fixture("negative_1_93.rs")
    assert "array_windows::<2>()" in negative
    assert "#[kani::proof]" not in negative
