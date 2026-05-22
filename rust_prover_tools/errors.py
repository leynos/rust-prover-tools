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

__all__ = ("CommandFailedError", "ProverToolError")


class ProverToolError(RuntimeError):
    """Base error for expected prover tool failures."""


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
