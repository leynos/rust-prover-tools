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

from cuprum import ExecutionContext, Program, ProgramCatalogue, ProjectSettings, sh

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
    """A command invocation request."""

    argv: tuple[str, ...]
    cwd: Path | None = None
    env: cabc.Mapping[str, str] | None = None
    echo: bool = False


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
        `cwd`, and echo behaviour.

    Returns
    -------
    CommandResult
        Captured standard output, standard error, and exit code.

    Raises
    ------
    ProverToolError
        Raised when `spec.argv` is empty.
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
    context = ExecutionContext(env=spec.env, cwd=spec.cwd)
    result = cmd.run_sync(echo=spec.echo, context=context)
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
