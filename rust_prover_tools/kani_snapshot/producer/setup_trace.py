"""The clean-setup descendant-process policy.

D6 and V8 permit setup to download the pinned prebuilt toolchain, extract the
bundle and probe versions. They forbid it from compiling Kani, a solver or any
other tool source. An adapter spy cannot see grandchildren, so the evidence is
an `strace -f -e trace=execve` trace.

The policy names the actions to refuse rather than the helpers to allow. A
new helper that extraction needs must not require a policy edit, while a new
route to a compiler must still be refused. The launcher's own `cargo-kani
setup` record is required, so a trace from an unrelated command cannot
discharge the claim.
"""

from __future__ import annotations

import dataclasses as dc
import json
import re
import typing as typ

from rust_prover_tools.errors import ProverToolError

if typ.TYPE_CHECKING:
    import collections.abc as cabc

__all__ = (
    "MAX_EXEC_RECORDS",
    "MAX_TRACE_BYTES",
    "ExecRecord",
    "SetupTraceError",
    "parse_exec_trace",
    "require_prebuilt_setup",
)

#: Largest trace accepted. A real setup trace is a few thousand short lines.
MAX_TRACE_BYTES: typ.Final[int] = 8 * 1024 * 1024

#: Most `execve` records accepted, so a hostile trace cannot exhaust memory.
MAX_EXEC_RECORDS: typ.Final[int] = 10_000

_EXECVE: typ.Final[re.Pattern[str]] = re.compile(
    r"^(?:(?:\[pid\s+[0-9]+\]\s+)|(?:[0-9]+\s+))?"
    r'execve\("(?P<path>(?:[^"\\]|\\.)*)",\s*\[(?P<argv>.*)\],\s*'
    # strace writes the environment as a pointer, optionally annotated with its
    # variable count, or as the whole variable list under `-v`.
    r"(?:0x[0-9a-f]+(?:\s*/\*\s*[0-9]+\s+vars?\s*\*/)?|\[[^\]]*\]|[0-9]+)?\s*\)"
)
_ARGUMENT: typ.Final[re.Pattern[str]] = re.compile(r'"((?:[^"\\]|\\.)*)"')
_ELIDED: typ.Final[re.Pattern[str]] = re.compile(r'"\s*\.\.\.')
#: strace writes a standalone `...` when it omits trailing argument entries.
_OMITTED: typ.Final[re.Pattern[str]] = re.compile(r"(?:^|[,\[])\s*\.\.\.\s*(?:,|$|\])")

#: Compiler backend children that also generate code.
_COMPILER_BACKENDS: typ.Final[frozenset[str]] = frozenset({"cc1", "cc1plus"})

#: Compilers, linkers and build drivers that must never run during setup.
_BUILD_TOOLS: typ.Final[frozenset[str]] = frozenset({
    "cc",
    "clang",
    "clang++",
    "cmake",
    "configure",
    "g++",
    "gcc",
    "gmake",
    "ld",
    "ld.lld",
    "lld",
    "make",
    "meson",
    "ninja",
    "pkg-config",
})

#: Shells whose argument vector can name a build driver.
_SHELLS: typ.Final[frozenset[str]] = frozenset({"bash", "dash", "ksh", "sh", "zsh"})

#: Build drivers a shell might be asked to run as a script.
_BUILD_SCRIPTS: typ.Final[frozenset[str]] = frozenset({
    "build",
    "build.sh",
    "configure",
    "gmake",
    "make",
})

#: Interpreters and the scripts that compile native code from source.
_PYTHONS: typ.Final[frozenset[str]] = frozenset({"python", "python3"})
_PYTHON_BUILD_SCRIPTS: typ.Final[frozenset[str]] = frozenset({
    "build.py",
    "setup.py",
})

#: `rustc` flags that turn a version probe into code generation.
_RUSTC_CODEGEN: typ.Final[tuple[str, ...]] = (
    "--crate-name",
    "--crate-type",
    "--emit",
    "-o",
)


class SetupTraceError(ProverToolError):
    """A setup trace shows a forbidden action or no usable evidence."""


@dc.dataclass(frozen=True, slots=True)
class ExecRecord:
    """One `execve` record from a descendant-process trace.

    Attributes
    ----------
    executable : str
        Absolute path strace reported.
    argv : tuple of str
        Argument vector, including the program name.
    """

    executable: str
    argv: tuple[str, ...]

    @property
    def name(self) -> str:
        """The basename of the executable, or of `argv[0]`."""
        return _basename(self.executable) or (
            _basename(self.argv[0]) if self.argv else ""
        )


def _basename(value: str) -> str:
    """Return the final path component without importing `os.path`."""
    return value.rsplit("/", 1)[-1]


def _unescape(value: str) -> str:
    """Decode one strace-quoted argument, falling back to its raw text.

    An argument strace elides is refused in `_argv`, before decoding.
    """
    try:
        decoded = json.loads(f'"{value}"')
    except json.JSONDecodeError:
        return value
    return decoded if isinstance(decoded, str) else value


def _argv(value: str) -> tuple[str, ...]:
    """Return the argument vector from strace's bracketed list.

    strace elides a long string as `"prefix"...` under its default string
    limit, which could hide a `.rs` suffix from the code-generation check. An
    elided argument is therefore refused rather than silently truncated.
    """
    if _ELIDED.search(value) is not None or _OMITTED.search(value) is not None:
        msg = f"strace elided an argument, so it cannot be checked: {value[:120]!r}"
        raise SetupTraceError(msg)
    return tuple(_unescape(item) for item in _ARGUMENT.findall(value))


def parse_exec_trace(trace: str) -> tuple[ExecRecord, ...]:
    """Return every `execve` record in a bounded strace trace.

    Parameters
    ----------
    trace : str
        Output of `strace -f -e trace=execve`, to a file or standard error.

    Returns
    -------
    tuple of ExecRecord
        Records in trace order. A line with no `execve` is ignored, and a
        failed `execve` is still a record because the attempt is the evidence.

    Raises
    ------
    SetupTraceError
        Raised when the trace is not text, exceeds the byte ceiling, or holds
        more records than the record ceiling.
    """
    _require_bounded_trace(trace)
    records: list[ExecRecord] = []
    for line in _joined(trace.splitlines()):
        record = _record(line)
        if record is not None:
            records.append(record)
        if len(records) > MAX_EXEC_RECORDS:
            msg = f"setup trace exceeds the {MAX_EXEC_RECORDS}-record ceiling"
            raise SetupTraceError(msg)
    return tuple(records)


_UNFINISHED: typ.Final[re.Pattern[str]] = re.compile(
    r"^(?P<pid>[0-9]+)\s+(?P<body>.*)<unfinished \.\.\.>$"
)
_RESUMED: typ.Final[re.Pattern[str]] = re.compile(
    r"^(?P<pid>[0-9]+)\s+<\.\.\. \w+ resumed>(?P<body>.*)$"
)


def _joined(lines: cabc.Iterable[str]) -> list[str]:
    """Join strace's unfinished/resumed fragments, keyed by process id.

    Concurrent descendants can make strace split one call across two lines.
    A fragment with no matching resume is refused, so a truncated trace is
    never mistaken for a complete one.
    """
    pending: dict[str, str] = {}
    joined: list[str] = []
    for line in lines:
        unfinished = _UNFINISHED.match(line)
        if unfinished is not None:
            pending[unfinished.group("pid")] = unfinished.group("body")
            continue
        resumed = _RESUMED.match(line)
        if resumed is not None:
            pid = resumed.group("pid")
            if pid not in pending:
                msg = f"setup trace resumes an unknown process {pid!r}: {line[:200]!r}"
                raise SetupTraceError(msg)
            tail = resumed.group("body").lstrip("> ")
            joined.append(f"{pid} {pending.pop(pid)}{tail}")
            continue
        joined.append(line)
    if pending:
        msg = f"setup trace has unfinished records: {sorted(pending)}"
        raise SetupTraceError(msg)
    return joined


def _require_bounded_trace(trace: str) -> None:
    """Require a text trace within the documented byte ceiling."""
    if not isinstance(trace, str):
        msg = "setup trace must be text"
        raise SetupTraceError(msg)
    if len(trace) > MAX_TRACE_BYTES:
        msg = f"setup trace exceeds the {MAX_TRACE_BYTES}-byte ceiling"
        raise SetupTraceError(msg)


def _record(line: str) -> ExecRecord | None:
    """Return the record one trace line states, or `None` for a non-execve line."""
    match = _EXECVE.match(line)
    if match is None:
        if "execve(" in line:
            msg = f"setup trace has an unparsable execve record: {line[:200]!r}"
            raise SetupTraceError(msg)
        return None
    return ExecRecord(
        executable=_unescape(match.group("path")),
        argv=_argv(match.group("argv")),
    )


def _forbidden(record: ExecRecord) -> str | None:
    """Return the forbidden action a record names, or `None` when allowed."""
    name = record.name
    if _is_named_forbidden(name):
        return name
    if _is_code_generation(name, record.argv[1:]):
        return name
    if _is_build_script(name, record.argv[1:]):
        return name
    return None


def _is_named_forbidden(name: str) -> bool:
    """Say whether the executable is itself a forbidden tool.

    GCC also installs versioned (`gcc-14`) and target-prefixed
    (`x86_64-linux-gnu-gcc`) names, and its backend child is `cc1`/`cc1plus`.
    The family is recognised by its last dash-separated field so those spellings
    cannot slip past an exact-name check.
    """
    if name in {"cargo", "rustdoc"}:
        return True
    if name in _BUILD_TOOLS:
        return True
    if name in _COMPILER_BACKENDS:
        return True
    return any(
        part in _BUILD_TOOLS or part in _COMPILER_BACKENDS for part in name.split("-")
    )


def _is_code_generation(name: str, arguments: tuple[str, ...]) -> bool:
    """Say whether a `rustc` invocation compiles rather than probes.

    A lone `-` makes rustc read source from standard input, and `@file` makes
    it read a whole argument vector from a file. Both can compile without
    naming a source file in the trace.
    """
    return name == "rustc" and _rustc_generates_code(arguments)


def _is_build_script(name: str, arguments: tuple[str, ...]) -> bool:
    """Say whether a shell or interpreter was asked to build source."""
    shell = name in _SHELLS and _runs_build_script(arguments)
    python = name in _PYTHONS and _runs_python_build(arguments)
    return shell or python


def _rustc_generates_code(arguments: tuple[str, ...]) -> bool:
    """Say whether a `rustc` invocation compiles rather than probes.

    A lone `-` makes rustc read source from standard input, and `@file` makes
    it read a whole argument vector from a file. Both can compile without
    naming a source file in the trace.
    """
    return any(
        argument == "-"
        or argument.startswith("@")
        or argument.endswith(".rs")
        or argument.startswith(_RUSTC_CODEGEN)
        for argument in arguments
    )


def _runs_build_script(arguments: tuple[str, ...]) -> bool:
    """Say whether a shell was asked to run a build driver."""
    return any(_basename(argument) in _BUILD_SCRIPTS for argument in arguments)


def _runs_python_build(arguments: tuple[str, ...]) -> bool:
    """Say whether an interpreter was asked to run a native build script."""
    return any(_basename(argument) in _PYTHON_BUILD_SCRIPTS for argument in arguments)


def require_prebuilt_setup(trace: str) -> tuple[ExecRecord, ...]:
    """Require a setup trace that consumed prebuilt bytes only.

    Parameters
    ----------
    trace : str
        Output of `strace -f -e trace=execve` covering one clean install.

    Returns
    -------
    tuple of ExecRecord
        The observed records, for evidence.

    Raises
    ------
    SetupTraceError
        Raised when the trace holds no record, shows no packaged
        `cargo-kani setup`, or names a forbidden build action.
    """
    records = parse_exec_trace(trace)
    _require_present(records)
    _require_allowed(records)
    _require_launcher_setup(records)
    return records


def _require_present(records: tuple[ExecRecord, ...]) -> None:
    """Require at least one record, so an empty trace cannot pass."""
    if not records:
        msg = "setup trace contains no execve records"
        raise SetupTraceError(msg)


def _require_allowed(records: tuple[ExecRecord, ...]) -> None:
    """Require every record to name an action setup may perform."""
    for record in records:
        forbidden = _forbidden(record)
        if forbidden is not None:
            msg = (
                f"setup executed forbidden build command {forbidden!r}: "
                f"{list(record.argv)}"
            )
            raise SetupTraceError(msg)


def _require_launcher_setup(records: tuple[ExecRecord, ...]) -> None:
    """Require the packaged launcher's own setup record."""
    if not _has_launcher_setup(records):
        msg = "setup trace does not show the packaged cargo-kani setup"
        raise SetupTraceError(msg)


def _has_launcher_setup(records: tuple[ExecRecord, ...]) -> bool:
    """Say whether the trace ran `cargo-kani setup`."""
    return any(
        record.name == "cargo-kani" and "setup" in record.argv[1:] for record in records
    )
