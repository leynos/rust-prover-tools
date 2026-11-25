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

Reading strace's text is `strace_records`; this module decides what the
records mean.
"""

from __future__ import annotations

import typing as typ

from rust_prover_tools.kani_snapshot.producer.strace_records import (
    MAX_EXEC_RECORDS,
    MAX_TRACE_BYTES,
    ExecRecord,
    SetupTraceError,
    basename,
    parse_exec_trace,
)

__all__ = (
    "MAX_EXEC_RECORDS",
    "MAX_TRACE_BYTES",
    "ExecRecord",
    "SetupTraceError",
    "parse_exec_trace",
    "require_prebuilt_setup",
)

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
    naming a source file in the trace. The remaining flags are the code
    generation `rustc` accepts without a `.rs` argument at all, so no argument
    here has to end in `.rs` for these to be refused.
    """
    return name == "rustc" and any(
        argument == "-"
        or argument.startswith("@")
        or argument.endswith(".rs")
        or argument.startswith(_RUSTC_CODEGEN)
        for argument in arguments
    )


def _is_build_script(name: str, arguments: tuple[str, ...]) -> bool:
    """Say whether a shell or interpreter was asked to build source."""
    shell = name in _SHELLS and _runs_build_script(arguments)
    python = name in _PYTHONS and _runs_python_build(arguments)
    return shell or python


def _runs_build_script(arguments: tuple[str, ...]) -> bool:
    """Say whether a shell was asked to run a build driver."""
    return any(basename(argument) in _BUILD_SCRIPTS for argument in arguments)


def _runs_python_build(arguments: tuple[str, ...]) -> bool:
    """Say whether an interpreter was asked to run a native build script."""
    return any(basename(argument) in _PYTHON_BUILD_SCRIPTS for argument in arguments)
