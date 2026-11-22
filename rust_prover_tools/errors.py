"""User-facing errors for prover tool workflows.

`ProverToolError` is the base class for expected validation, configuration, and
workflow failures. `CommandFailedError` specialises it for external commands
that exit non-zero after being invoked through the Cuprum command wrapper.

Example
-------
```python
raise CommandFailedError(
    command_line="cargo kani --version",
    exit_code=2,
    stderr="cargo failed",
)
```
"""

from __future__ import annotations

__all__ = (
    "CommandFailedError",
    "CommandTimedOutError",
    "CommandTimeoutError",
    "ProverToolError",
    "TransientDownloadError",
)


class ProverToolError(RuntimeError):
    """Base error for expected prover tool failures."""


class TransientDownloadError(ProverToolError):
    """A transport failure that a bounded retry may resolve.

    Integrity, policy and local filesystem failures deliberately use the base
    class so a retry can never mask them as a flaky connection.
    """


class CommandTimeoutError(ProverToolError):
    """Raised when a command exceeds its wall-clock budget.

    A timed-out command's whole process group has already been terminated
    before this is raised. Callers that may retry a timeout catch this type
    rather than the broader base class so a genuine command failure is never
    mistaken for one.
    """


class CommandTimedOutError(CommandTimeoutError):
    """Raised when an external command exceeds its wall-clock budget.

    Attributes
    ----------
    command_line : str
        Human-readable command line that expired.
    timeout_seconds : float
        Effective budget that the command exceeded. This may come from the
        command specification or from an enclosing Cuprum scope.
    argv : tuple[str, ...]
        Argument vector that was executed, without shell interpretation.
    """

    def __init__(
        self,
        command_line: str,
        timeout_seconds: float,
        argv: tuple[str, ...],
    ) -> None:
        """Initialise the timeout failure.

        Parameters
        ----------
        command_line : str
            The command line that expired.
        timeout_seconds : float
            Effective timeout that applied to the command.
        argv : tuple[str, ...]
            The executed argument vector.
        """
        super().__init__(
            f"command timed out after {timeout_seconds:g} seconds: {command_line}"
        )
        self.command_line = command_line
        self.timeout_seconds = timeout_seconds
        self.argv = argv


class CommandFailedError(ProverToolError):
    """Raised when an external command exits with a non-zero status."""

    def __init__(self, command_line: str, exit_code: int, stderr: str) -> None:
        """Initialise the command failure.

        Parameters
        ----------
        command_line : str
            The command line that failed.
        exit_code : int
            The process exit code.
        stderr : str
            Captured standard error.
        """
        stripped = stderr.strip()
        suffix = f": {stripped}" if stripped else ""
        super().__init__(f"command failed ({exit_code}): {command_line}{suffix}")
        self.command_line = command_line
        self.exit_code = exit_code
        self.stderr = stderr
