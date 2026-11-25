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


def test_an_unparsable_execve_record_is_refused() -> None:
    """A truncated execve record is evidence of a broken capture, not noise."""
    trace = _LAUNCHER + '901  execve("/bin/foo", ["foo"], 0x7ffd2c1a4b30'

    with pytest.raises(SetupTraceError, match="unparsable"):
        require_prebuilt_setup(trace)


#: Records captured from real `strace -f -qq -s 4096 -e trace=execve` output.
_REAL_ALLOWED = (
    '2576251 execve("/bin/sh", ["/bin/sh", "-c", "true"], '
    "0x7ffc5b3c5958 /* 109 vars */) = 0\n"
)
_REAL_ALLOWED_WITH_ENV = (
    '2576257 execve("/usr/bin/env", ["env"], ["HOME=/root", "PATH=/usr/bin"]) = 0\n'
)


def test_a_real_strace_record_with_an_environment_comment_is_accepted() -> None:
    """A captured record annotates the environment pointer with its count."""
    trace = _LAUNCHER + _REAL_ALLOWED

    records = require_prebuilt_setup(trace)

    assert records[1].executable == "/bin/sh"
    assert records[1].argv == ("/bin/sh", "-c", "true")


def test_a_real_strace_record_with_a_full_environment_is_accepted() -> None:
    """`strace -v` writes the environment as a list instead of a pointer."""
    trace = _LAUNCHER + _REAL_ALLOWED_WITH_ENV

    records = require_prebuilt_setup(trace)

    assert records[1].argv == ("env",)


def test_a_standalone_omitted_argument_marker_is_refused() -> None:
    """`["rustc", ...]` hides an argument that could name source code."""
    trace = (
        _LAUNCHER + '901  execve("/toolchain/bin/rustc", ["rustc", ...], '
        "0x7ffd2c1a4b30) = 0\n"
    )

    with pytest.raises(SetupTraceError, match="elided"):
        require_prebuilt_setup(trace)


def test_an_elided_argument_is_refused() -> None:
    """An argument strace elides could hide a `.rs` suffix."""
    trace = (
        _LAUNCHER + '901  execve("/toolchain/bin/rustc", ["rustc", '
        '"/home/runner/work/a-very-long-path/that/strace/elides/source.rs"...], '
        "0x7ffd2c1a4b30) = 0\n"
    )

    with pytest.raises(SetupTraceError, match="elided"):
        require_prebuilt_setup(trace)


def test_rustc_reading_source_from_stdin_is_refused() -> None:
    """`rustc -` compiles standard input without naming a source file."""
    trace = (
        _LAUNCHER
        + '901  execve("/toolchain/bin/rustc", ["rustc", "-"], 0x7ffd2c1a4b30) = 0\n'
    )

    with pytest.raises(SetupTraceError, match="rustc"):
        require_prebuilt_setup(trace)


def test_fragments_from_concurrent_children_are_joined() -> None:
    """A trace can split one call when another descendant runs meanwhile."""
    trace = (
        '901  execve("/bin/sh", ["sh", "-c", "true"], 0x123 <unfinished ...>\n'
        '902  execve("/usr/bin/tar", ["tar", "-xf", "t"], 0x456) = 0\n'
        "901  <... execve resumed>) = 0\n"
    )

    records = parse_exec_trace(trace)

    # A joined record is emitted where its resume completes, so it follows the
    # record that ran in between; the set and each argv are what matter.
    assert {record.executable for record in records} == {"/bin/sh", "/usr/bin/tar"}
    shell = next(record for record in records if record.executable == "/bin/sh")
    assert shell.argv == ("sh", "-c", "true")


def test_bracketed_pid_fragments_are_joined() -> None:
    """A stderr trace prefixes each fragment with `[pid N]`."""
    trace = (
        '[pid 901] execve("/bin/sh", ["sh", "-c", "true"], 0x1 <unfinished ...>\n'
        '[pid 902] execve("/usr/bin/tar", ["tar"], 0x2) = 0\n'
        "[pid 901] <... execve resumed>) = 0\n"
    )

    records = parse_exec_trace(trace)

    assert {record.name for record in records} == {"sh", "tar"}


def test_a_bracketed_resume_without_a_fragment_is_refused() -> None:
    """A bracketed resume with no start is a broken capture too."""
    trace = "[pid 901] <... execve resumed>) = 0\n"

    with pytest.raises(SetupTraceError, match="unknown process"):
        parse_exec_trace(trace)


def test_an_unfinished_record_without_a_resume_is_refused() -> None:
    """A truncated fragment is not evidence of a completed call."""
    trace = '901  execve("/bin/sh", ["sh"], 0x123 <unfinished ...>\n'

    with pytest.raises(SetupTraceError, match="unfinished"):
        parse_exec_trace(trace)


def test_a_second_unfinished_fragment_for_one_pid_is_refused() -> None:
    """Replacing a fragment would silently drop a compiler attempt."""
    trace = (
        '901  execve("/usr/bin/gcc", ["gcc", "-c", "a.c"], 0x1 <unfinished ...>\n'
        '901  execve("/usr/bin/tar", ["tar"], 0x2 <unfinished ...>\n'
        "901  <... execve resumed>) = 0\n"
    )

    with pytest.raises(SetupTraceError, match="more than one unfinished"):
        parse_exec_trace(trace)


def test_a_resume_without_a_fragment_is_refused() -> None:
    """A resume with no matching start is a broken capture."""
    trace = "901  <... execve resumed>) = 0\n"

    with pytest.raises(SetupTraceError, match="unknown process"):
        parse_exec_trace(trace)


@pytest.mark.parametrize(
    "compiler", ["gcc-14", "x86_64-linux-gnu-gcc", "cc1", "cc1plus"]
)
def test_versioned_and_prefixed_compiler_names_are_refused(compiler: str) -> None:
    """GCC's versioned, target-prefixed and backend names still compile code."""
    trace = (
        _LAUNCHER
        + f'901  execve("/usr/bin/{compiler}", ["{compiler}", "-c", "a.c"], 0x1) = 0\n'
    )

    with pytest.raises(SetupTraceError, match="forbidden"):
        require_prebuilt_setup(trace)


def test_a_rustc_response_file_is_refused() -> None:
    """`rustc @args` can name code generation inside an unread file."""
    response_file = (
        '901  execve("/toolchain/bin/rustc", '
        '["rustc", "@/tmp/compile.args"], 0x1) = 0\n'
    )
    trace = _LAUNCHER + response_file

    with pytest.raises(SetupTraceError, match="rustc"):
        require_prebuilt_setup(trace)


@pytest.mark.parametrize(
    "arguments",
    [
        ("--emit=link",),
        ("-o", "out"),
        ("--crate-type", "lib"),
        ("--crate-name", "probe"),
    ],
    ids=["emit", "output", "crate-type", "crate-name"],
)
def test_rustc_code_generation_without_a_source_file_is_refused(
    arguments: tuple[str, ...],
) -> None:
    """Each code-generation flag is refused on its own, with no `.rs` argument.

    None of these vectors ends in `.rs`, so the refusal can only come from the
    flag clause; removing it would make every case here pass.
    """
    trace = (
        _LAUNCHER
        + '901  execve("/toolchain/bin/rustc", ["rustc", '
        + ", ".join(f'"{argument}"' for argument in arguments)
        + "], 0x1) = 0\n"
    )

    with pytest.raises(SetupTraceError, match="rustc"):
        require_prebuilt_setup(trace)


@pytest.mark.parametrize(
    ("executable", "arguments"),
    [
        ("/bin/sh", ("sh", "build.sh")),
        ("/bin/bash", ("bash", "-c", "make")),
        ("/usr/bin/python3", ("python3", "setup.py", "build_ext")),
        ("/usr/bin/python", ("python", "build.py")),
    ],
    ids=["sh-script", "bash-make", "python3-setup", "python-build"],
)
def test_a_shell_or_interpreter_build_is_refused(
    executable: str, arguments: tuple[str, ...]
) -> None:
    """A shell or interpreter asked to build source is refused and named.

    Each vector is a separate route to a build (`sh build.sh`, `bash -c make`,
    `python3 setup.py build_ext`, `python build.py`), so removing the shell or
    interpreter clause leaves its own case passing.
    """
    argv = ", ".join(f'"{argument}"' for argument in arguments)
    trace = _LAUNCHER + f'901  execve("{executable}", [{argv}], 0x1) = 0\n'

    with pytest.raises(SetupTraceError, match="forbidden"):
        require_prebuilt_setup(trace)


@pytest.mark.parametrize(
    ("escape", "message"),
    [("\\xZZ", "malformed escape"), ("\\777", "out-of-range")],
    ids=["non-hex", "octal-overflow"],
)
def test_a_malformed_escape_is_refused(escape: str, message: str) -> None:
    """A damaged escape fails loudly rather than being read as raw text.

    The raw text fallback this replaced would have compared the undecoded
    escape sequence against the policy's lists, so an argument could be
    checked as something other than what strace recorded.
    """
    trace = (
        _LAUNCHER
        + f'901  execve("/usr/bin/tar", ["tar", "bad{escape}escape"], 0x1) = 0\n'
    )

    with pytest.raises(SetupTraceError, match=message):
        parse_exec_trace(trace)


def test_an_octal_escaped_utf8_path_is_decoded() -> None:
    r"""Strace writes a non-ASCII byte as an octal escape, not a JSON escape.

    `caf\303\251` is strace's spelling of `café`. Refusing it would fail an
    installation under such a path, so the reader must follow strace's C-string
    format rather than JSON's.
    """
    trace = _LAUNCHER + (
        '901  execve("/home/caf\\303\\251/toolchain/bin/rustc", '
        '["rustc", "-vV"], 0x1) = 0\n'
    )

    records = parse_exec_trace(trace)

    assert records[1].executable == "/home/café/toolchain/bin/rustc"


def test_a_c_escaped_control_character_is_decoded() -> None:
    r"""A named C escape such as `\t` is decoded to the character it denotes."""
    trace = _LAUNCHER + (
        '901  execve("/usr/bin/tar", ["tar", "a\\tb", "c\\\\d", "e\\"f"], 0x1) = 0\n'
    )

    records = parse_exec_trace(trace)

    assert records[1].argv == ("tar", "a\tb", "c\\d", 'e"f')


@pytest.mark.parametrize(
    "field",
    [
        '"rustc", @compile.args',
        '"rustc", "--emit=link" @compile.args',
        '"rustc", @compile.args, "-vV"',
        '"rustc", bare-token',
    ],
    ids=["leading", "no-separator", "trailing", "unquoted"],
)
def test_an_unreadable_argument_field_is_refused(field: str) -> None:
    """Every token must be a quoted argument, or the record cannot be checked.

    `re.findall` alone would return only `"rustc"` here and silently drop
    `@compile.args`, so a response-file compile could pass the policy as a
    bare version probe.
    """
    trace = _LAUNCHER + f'901  execve("/toolchain/bin/rustc", [{field}], 0x1) = 0\n'

    with pytest.raises(SetupTraceError, match="unreadable argument list"):
        parse_exec_trace(trace)


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
