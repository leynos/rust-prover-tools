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
    r"(?:0x[0-9a-f]+|[0-9]+)\)"
)
_ARGUMENT: typ.Final[re.Pattern[str]] = re.compile(r'"((?:[^"\\]|\\.)*)"')

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
    """Decode one strace-quoted argument, falling back to its raw text."""
    try:
        decoded = json.loads(f'"{value}"')
    except json.JSONDecodeError:
        return value
    return decoded if isinstance(decoded, str) else value


def _argv(value: str) -> tuple[str, ...]:
    """Return the argument vector from strace's bracketed list."""
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
    if not isinstance(trace, str):
        msg = "setup trace must be text"
        raise SetupTraceError(msg)
    if len(trace) > MAX_TRACE_BYTES:
        msg = f"setup trace exceeds the {MAX_TRACE_BYTES}-byte ceiling"
        raise SetupTraceError(msg)
    records: list[ExecRecord] = []
    for line in trace.splitlines():
        match = _EXECVE.match(line)
        if match is None:
            continue
        records.append(
            ExecRecord(
                executable=_unescape(match.group("path")),
                argv=_argv(match.group("argv")),
            )
        )
        if len(records) > MAX_EXEC_RECORDS:
            msg = f"setup trace exceeds the {MAX_EXEC_RECORDS}-record ceiling"
            raise SetupTraceError(msg)
    return tuple(records)


def _forbidden(record: ExecRecord) -> str | None:
    """Return the forbidden action a record names, or `None` when allowed."""
    name = record.name
    if name in {"cargo", "rustdoc"}:
        return name
    if name == "rustc" and _rustc_generates_code(record.argv[1:]):
        return name
    if name in _BUILD_TOOLS:
        return name
    if name in _SHELLS and _runs_build_script(record.argv[1:]):
        return name
    if name in _PYTHONS and _runs_python_build(record.argv[1:]):
        return name
    return None


def _rustc_generates_code(arguments: tuple[str, ...]) -> bool:
    """Say whether a `rustc` invocation compiles rather than probes."""
    return any(
        argument.endswith(".rs") or argument.startswith(_RUSTC_CODEGEN)
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
    if not records:
        msg = "setup trace contains no execve records"
        raise SetupTraceError(msg)
    for record in records:
        forbidden = _forbidden(record)
        if forbidden is not None:
            msg = (
                f"setup executed forbidden build command {forbidden!r}: "
                f"{list(record.argv)}"
            )
            raise SetupTraceError(msg)
    if not any(
        record.name == "cargo-kani" and "setup" in record.argv[1:] for record in records
    ):
        msg = "setup trace does not show the packaged cargo-kani setup"
        raise SetupTraceError(msg)
    return records
