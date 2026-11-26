"""Unit tests for the consumer smoke's evidence readers.

These helpers read text the CLI and `strace` produce, so they can be tested
without installing a snapshot. The generation-path reader in particular sees a
path the CLI renders unescaped, which may contain spaces.
"""

from __future__ import annotations

import typing as typ

import pytest
from _pytest.outcomes import Failed

from tests import (
    kani_snapshot_smoke_commands,
    kani_snapshot_smoke_evidence,
    kani_snapshot_smoke_runner,
)
from tests.kani_snapshot_smoke_commands import require_strace, strace_prefix
from tests.kani_snapshot_smoke_evidence import generation_from_output, read_trace

if typ.TYPE_CHECKING:
    from pathlib import Path


def test_a_generation_path_with_spaces_is_captured_whole() -> None:
    """A cache below a directory with a space yields a path with a space."""
    path = "/home/tester/kani smoke/.kani-snapshots/abc/generations/gen"
    text = f"snapshot kani-snapshot-2026-11-0d2328a93f0e-r1 installed at {path}\n"

    assert generation_from_output(text) == path


def test_an_ordinary_generation_path_is_captured() -> None:
    """The common path form is unchanged."""
    text = "snapshot tag checked at /cache/abc/generations/gen\n"

    assert generation_from_output(text) == "/cache/abc/generations/gen"


def test_output_without_a_generation_line_yields_none() -> None:
    """A failed install names no generation."""
    assert generation_from_output("error: the payload digest differs\n") is None


def test_a_trace_at_the_ceiling_is_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A trace exactly at the ceiling is accepted."""
    monkeypatch.setattr(kani_snapshot_smoke_evidence, "MAX_TRACE_BYTES", 16)
    trace = tmp_path / "small.trace"
    trace.write_bytes(b"x" * 16)

    assert read_trace(trace) == "x" * 16


def test_a_trace_over_the_ceiling_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A trace larger than the ceiling is refused rather than truncated."""
    monkeypatch.setattr(kani_snapshot_smoke_evidence, "MAX_TRACE_BYTES", 8)
    trace = tmp_path / "large.trace"
    trace.write_bytes(b"x" * 9)

    with pytest.raises(AssertionError, match="ceiling"):
        read_trace(trace)


def test_output_over_the_capture_ceiling_fails_the_smoke(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A truncated log is refused, because an incomplete transcript proves nothing.

    Only a bound is loaded into memory, so the test asserts the refusal rather
    than the captured text.
    """
    monkeypatch.setattr(kani_snapshot_smoke_runner, "MAX_CAPTURE_BYTES", 8)

    with pytest.raises(Failed, match="incomplete transcript"):
        kani_snapshot_smoke_runner.run(
            ["/bin/sh", "-c", "printf '0123456789abcdef'"],
            kani_snapshot_smoke_runner.SmokeContext(cwd=tmp_path),
        )


def test_the_strace_prefix_requests_complete_arguments(tmp_path: Path) -> None:
    """The prefix raises strace's string limit and stays quiet."""
    trace = tmp_path / "trace"
    prefix = strace_prefix("/usr/bin/strace", trace)

    assert "-qq" in prefix
    assert prefix[prefix.index("-s") + 1] == "4096"
    assert prefix[-1] == str(trace)


def test_require_strace_returns_the_resolved_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The helper returns whatever discovery resolves, without needing strace.

    The real prerequisite belongs to the opt-in smoke, which reaches this
    helper through `traced_run`; an ordinary `make test` must not depend on the
    tool being installed.
    """
    monkeypatch.setattr(
        kani_snapshot_smoke_commands.shutil,
        "which",
        lambda name: f"/usr/bin/{name}",
    )

    assert require_strace() == "/usr/bin/strace"
