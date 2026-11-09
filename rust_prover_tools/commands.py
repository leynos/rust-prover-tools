"""Cuprum-backed external command execution helpers.

This module centralizes all subprocess-style execution for the prover CLI.
Callers describe work with `CommandSpec`, use `split_command` and
`command_line` for safe argument handling and display, then execute through
`run_command` or `run_checked`. The helpers keep production execution and
cmd-mox-backed tests on the same Cuprum path.

Example
-------
```python
from rust_prover_tools.commands import CommandSpec, run_checked

result = run_checked(CommandSpec(("cargo", "kani", "--version")))
print(result.stdout)
```
"""

from __future__ import annotations

import dataclasses as dc
import shlex
import typing as typ

from cuprum import (
    BrokenPipePolicy,
    ExecutionContext,
    ProcessGroupPolicy,
    Program,
    ProgramCatalogue,
    ProjectSettings,
    RunOutputOptions,
    TimeoutExpired,
    current_context,
    sh,
)
from cuprum.context import EnvMode, UnsetType

from rust_prover_tools.errors import CommandFailedError, ProverToolError

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

__all__ = (
    "CommandResult",
    "CommandSpec",
    "command_line",
    "run_checked",
    "run_command",
    "split_command",
)


@dc.dataclass(frozen=True, slots=True)
class CommandResult:
    """Captured command result used by prover workflows."""

    stdout: str
    stderr: str
    exit_code: int


@dc.dataclass(frozen=True, slots=True)
class CommandSpec:
    """A command invocation request.

    Attributes
    ----------
    argv : tuple[str, ...]
        Program and arguments to execute.
    cwd : Path | None
        Working directory for the child process.
    env : collections.abc.Mapping[str, str | UnsetType] | None
        Environment policy for the child process, interpreted according to
        `env_mode`. A `cuprum.UNSET` value removes an inherited variable
        without mutating this process's environment.
    env_mode : cuprum.EnvMode
        How `env` relates to the child's inherited environment. The default
        `OVERLAY` layers the mapping over the live environment; `REPLACE`
        starts from an empty environment. Callers that must not pass a host
        selector to the child need `OVERLAY` with `UNSET` markers or
        `REPLACE`; an overlay alone cannot remove a variable.
    echo : bool
        Mirror the child's output to the caller's streams while capturing it.
    timeout_seconds : float | None
        Wall-clock budget for the whole child process tree. `None` leaves the
        command unbounded unless an enclosing Cuprum scope supplies a default
        timeout. Whenever an effective timeout applies, the child is placed in
        its own process group so expiry terminates every descendant it
        started.
    """

    argv: tuple[str, ...]
    cwd: Path | None = None
    env: cabc.Mapping[str, str | UnsetType] | None = None
    env_mode: EnvMode = EnvMode.OVERLAY
    echo: bool = False
    timeout_seconds: float | None = None


def split_command(command: str) -> tuple[str, ...]:
    """Split a user-supplied command string using shell-like quoting rules.

    Parameters
    ----------
    command : str
        Command string parsed with `shlex.split`.

    Returns
    -------
    tuple[str, ...]
        Argument vector tokens.

    Raises
    ------
    ProverToolError
        Raised when parsing fails or the command is empty.
    """
    try:
        argv = tuple(shlex.split(command))
    except ValueError as exc:
        msg = f"could not parse command '{command}': {exc}"
        raise ProverToolError(msg) from exc
    if not argv:
        msg = "command is empty"
        raise ProverToolError(msg)
    return argv


def command_line(argv: cabc.Sequence[str]) -> str:
    """Return a shell-readable display string for an argument vector.

    Parameters
    ----------
    argv : Sequence[str]
        Argument vector to format.

    Returns
    -------
    str
        Shell-quoted command line string.
    """
    return " ".join(shlex.quote(arg) for arg in argv)


def run_command(spec: CommandSpec) -> CommandResult:
    """Run a command through Cuprum and capture stdout, stderr, and exit code.

    Parameters
    ----------
    spec : CommandSpec
        Command specification containing `argv`, optional `env`, optional
        `cwd`, echo behaviour and an optional wall-clock timeout.

    Returns
    -------
    CommandResult
        Captured standard output, standard error, and exit code.

    Raises
    ------
    ProverToolError
        Raised when `spec.argv` is empty or the command exceeds its timeout.
        A timed-out command's whole process group is terminated first, so a
        setup tool cannot outlive the caller's budget by leaving descendants.
    """
    if not spec.argv:
        msg = "command is empty"
        raise ProverToolError(msg)

    program = Program(spec.argv[0])
    # Cuprum's catalogue expects project metadata even for dynamically
    # allowlisted single-program invocations.
    project = ProjectSettings(
        name="rust-prover-tools",
        programs=(program,),
        documentation_locations=("docs/developers-guide.md",),
        noise_rules=(),
    )
    catalogue = ProgramCatalogue(projects=(project,))
    cmd = sh.make(program, catalogue=catalogue)(*spec.argv[1:])
    # Cuprum falls back to the enclosing scope's default timeout when the spec
    # has none. Resolve that here because both the process-group choice and
    # the user-facing message depend on the timeout that will really apply.
    effective_timeout = spec.timeout_seconds
    if effective_timeout is None:
        effective_timeout = current_context().timeout
    context = ExecutionContext(
        env=spec.env,
        env_mode=spec.env_mode,
        cwd=spec.cwd,
        timeout=effective_timeout,
        # Only a bounded command needs its descendants terminated with it;
        # unbounded calls keep Cuprum's inherited-group default.
        process_group=(
            ProcessGroupPolicy.OWN_GROUP
            if effective_timeout is not None
            else ProcessGroupPolicy.INHERIT
        ),
    )
    # Echo mirrors output the user asked to see, such as a Verus proof run, so
    # the reader is often a pipe. `BEST_EFFORT` keeps a reader that goes away
    # (`| head`, a quit pager) from failing a command that succeeded, while
    # capture and the child's exit status still reach the caller.
    output = RunOutputOptions(
        echo=spec.echo,
        broken_pipe_policy=BrokenPipePolicy.BEST_EFFORT,
    )
    try:
        result = cmd.run_sync(output=output, context=context)
    except TimeoutExpired as exc:
        # `exc.timeout` is always the effective budget, so a timeout supplied
        # by an enclosing scope is reported correctly too.
        msg = (
            f"command timed out after {exc.timeout:g} seconds: "
            f"{command_line(spec.argv)}"
        )
        raise ProverToolError(msg) from exc
    return CommandResult(
        stdout=result.stdout or "",
        stderr=result.stderr or "",
        exit_code=result.exit_code,
    )


def run_checked(spec: CommandSpec) -> CommandResult:
    """Run a command and raise a user-facing error if it fails.

    Parameters
    ----------
    spec : CommandSpec
        Command specification passed to `run_command`.

    Returns
    -------
    CommandResult
        Captured output for successful command execution.

    Raises
    ------
    CommandFailedError
        Raised when the command exits non-zero. The error message uses
        `command_line(spec.argv)` for stable display.
    """
    result = run_command(spec)
    if result.exit_code != 0:
        raise CommandFailedError(
            command_line(spec.argv),
            result.exit_code,
            result.stderr,
        )
    return result
