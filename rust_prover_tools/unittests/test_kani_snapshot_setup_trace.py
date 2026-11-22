"""Tests for the clean-setup descendant-process policy.

D6 and V8 allow setup to download and install the pinned prebuilt toolchain and
to probe versions, but forbid it from compiling Kani, a solver or any other
tool source. An adapter spy cannot see grandchildren, so the evidence is an
`strace -f -e trace=execve` trace. These tests pin the policy against recorded
traces and generated orderings, including a launcher-setup non-vacuity guard.
"""

from __future__ import annotations

import pathlib
import typing as typ

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from rust_prover_tools.kani_snapshot.producer.setup_trace import (
    MAX_TRACE_BYTES,
    SetupTraceError,
    parse_exec_trace,
    require_prebuilt_setup,
)

FIXTURES: typ.Final[pathlib.Path] = (
    pathlib.Path(__file__).resolve().parents[2]
    / "tests"
    / "fixtures"
    / "kani_snapshot"
    / "transcripts"
)

_LAUNCHER = (
    '900  execve("/gen/bin/cargo-kani", '
    '["cargo-kani", "setup", "--use-local-bundle", "/tmp/bundle.tar.gz"], '
    "0x7ffd2c1a4b30) = 0\n"
)
_ALLOWED_GH = (
    '901  execve("/usr/bin/gh", '
    '["gh", "attestation", "verify", "/tmp/manifest.json"], '
    "0x7ffd2c1a4b30) = 0\n"
)
_ALLOWED_RUSTUP = (
    '902  execve("/usr/bin/rustup", '
    '["rustup", "toolchain", "install", "nightly-2026-08-21"], '
    "0x7ffd2c1a4b30) = 0\n"
)
_ALLOWED_TAR = (
    '903  execve("/usr/bin/tar", ["tar", "-xzf", "/tmp/t.tar.gz"], '
    "0x7ffd2c1a4b30) = 0\n"
)
_ALLOWED_RUSTC = (
    '904  execve("/toolchain/bin/rustc", ["rustc", "-vV"], 0x7ffd2c1a4b30) = 0\n'
)
_ALLOWED_COMPILER = (
    '905  execve("/gen/bin/kani-compiler", ["kani-compiler", "--version"], '
    "0x7ffd2c1a4b30) = 0\n"
)
_ALLOWED = (
    _ALLOWED_GH,
    _ALLOWED_RUSTUP,
    _ALLOWED_TAR,
    _ALLOWED_RUSTC,
    _ALLOWED_COMPILER,
)
_FORBIDDEN = (
    '999  execve("/home/runner/.cargo/bin/cargo", ["cargo", "install"], '
    "0x7ffd2c1a4b30) = 0\n"
)


def _fixture(name: str) -> str:
    """Return one recorded trace as text."""
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_the_allowed_trace_is_accepted_and_reports_its_records() -> None:
    """The allowed trace names the launcher setup and no forbidden action."""
    records = require_prebuilt_setup(_fixture("setup_allowed.txt"))

    assert len(records) == 7
    assert records[1].executable.endswith("cargo-kani")
    assert records[1].argv[:2] == ("cargo-kani", "setup")


@pytest.mark.parametrize(
    ("name", "offender"),
    [
        ("setup_cargo_build.txt", "cargo"),
        ("setup_rustc_codegen.txt", "rustc"),
        ("setup_make.txt", "make"),
    ],
)
def test_a_source_build_is_refused_by_name(name: str, offender: str) -> None:
    """Each seeded build action is refused and named in the message."""
    with pytest.raises(SetupTraceError, match=offender):
        require_prebuilt_setup(_fixture(name))


def test_a_trace_without_the_packaged_setup_is_refused() -> None:
    """A trace from an unrelated command cannot discharge the setup claim."""
    with pytest.raises(SetupTraceError, match="cargo-kani setup"):
        require_prebuilt_setup(_fixture("setup_no_launcher.txt"))


def test_an_empty_trace_is_refused() -> None:
    """An strace log without an execve record proves nothing."""
    with pytest.raises(SetupTraceError, match="no execve"):
        require_prebuilt_setup(_fixture("setup_empty.txt"))


def test_the_bracketed_pid_form_is_accepted() -> None:
    """`strace -f` to stderr writes `[pid N]` instead of a bare pid."""
    trace = (
        '[pid  900] execve("/gen/bin/cargo-kani", '
        '["cargo-kani", "setup", "--use-local-bundle", "/tmp/b.tar.gz"], '
        "0x7ffd2c1a4b30) = 0\n"
        '[pid  901] execve("/toolchain/bin/rustc", ["rustc", "-vV"], '
        "0x7ffd2c1a4b30) = 0\n"
    )

    records = require_prebuilt_setup(trace)

    assert len(records) == 2


def test_a_failed_execve_of_a_forbidden_tool_is_still_refused() -> None:
    """An attempted build is forbidden even when the executable is absent."""
    trace = (
        _LAUNCHER + '901  execve("/home/runner/.cargo/bin/cargo", ["cargo", "build"], '
        "0x7ffd2c1a4b30) = -1 ENOENT (No such file or directory)\n"
    )

    with pytest.raises(SetupTraceError, match="cargo"):
        require_prebuilt_setup(trace)


def test_an_oversized_trace_is_refused_before_parsing() -> None:
    """The bounded reader refuses a trace larger than its ceiling."""
    with pytest.raises(SetupTraceError, match="ceiling"):
        parse_exec_trace("x" * (MAX_TRACE_BYTES + 1))


@given(extra=st.lists(st.sampled_from(_ALLOWED), max_size=6))
@settings(max_examples=25, deadline=None)
def test_allowed_records_pass_in_any_order(extra: list[str]) -> None:
    """The policy is a record predicate, not a check on a fixed sequence."""
    trace = _LAUNCHER + "".join(extra)

    records = require_prebuilt_setup(trace)

    assert records[0].argv[:2] == ("cargo-kani", "setup")


@given(
    allowed=st.lists(st.sampled_from(_ALLOWED), max_size=4),
    position=st.integers(min_value=0, max_value=4),
)
@settings(max_examples=25, deadline=None)
def test_one_forbidden_record_is_refused_at_any_position(
    allowed: list[str], position: int
) -> None:
    """A single seeded build action is detected wherever it appears."""
    lines = [*allowed]
    lines.insert(min(position, len(lines)), _FORBIDDEN)
    trace = _LAUNCHER + "".join(lines)

    with pytest.raises(SetupTraceError, match="cargo"):
        require_prebuilt_setup(trace)
