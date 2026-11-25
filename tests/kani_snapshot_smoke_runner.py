"""The opt-in consumer smoke's one bounded process runner.

Every public command the smoke invokes goes through `run`, so its streams are
captured to temporary files and only a bounded tail is loaded. A proof log is
therefore never held whole in memory, and the validator ceilings apply to text
the caller already had to bound.
"""

from __future__ import annotations

import dataclasses as dc
import os
import subprocess
import tempfile
import typing as typ

import pytest

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

__all__ = (
    "MAX_CAPTURE_BYTES",
    "PROOF_TIMEOUT",
    "SmokeContext",
    "run",
)

#: How long one real proof may take, in seconds.
PROOF_TIMEOUT: typ.Final[int] = 1800

#: How much of a command's output is retained for diagnostics. The launcher
#: mirrors output live; only this tail is loaded into memory.
MAX_CAPTURE_BYTES: typ.Final[int] = 1024 * 1024


@dc.dataclass(frozen=True, slots=True)
class SmokeContext:
    """One command invocation's working directory and bounds.

    Attributes
    ----------
    cwd : Path
        Working directory for the child.
    env : collections.abc.Mapping of str to str, optional
        Variables added to the inherited environment.
    timeout : int
        Seconds the child may run.
    check : bool
        Whether a non-zero exit fails the test immediately.
    replace_env : bool
        When true, `env` is the child's whole environment rather than an
        overlay on the caller's.
    """

    cwd: Path
    env: cabc.Mapping[str, str] | None = None
    timeout: int = PROOF_TIMEOUT
    check: bool = True
    replace_env: bool = False


def _capture(handle: typ.BinaryIO) -> tuple[str, bool]:
    """Return a captured stream and whether it exceeded the ceiling."""
    handle.seek(0, os.SEEK_END)
    size = handle.tell()
    truncated = size > MAX_CAPTURE_BYTES
    handle.seek(max(0, size - MAX_CAPTURE_BYTES))
    return handle.read().decode("utf-8", errors="replace"), truncated


def run(command: list[str], context: SmokeContext) -> subprocess.CompletedProcess[str]:
    """Run one public command with bounded capture.

    The child's streams go to temporary files, and only their tails are
    loaded. A proof log is therefore never held whole in memory, and the
    validator ceilings apply to text the caller already had to bound.
    """
    environment = (
        dict(context.env or {})
        if context.replace_env
        else {**os.environ, **(context.env or {})}
    )
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        completed = subprocess.run(
            command,
            cwd=context.cwd,
            env=environment,
            stdout=out,
            stderr=err,
            timeout=context.timeout,
            check=False,
        )
        stdout, stdout_truncated = _capture(out)
        stderr, stderr_truncated = _capture(err)
    if stdout_truncated or stderr_truncated:
        pytest.fail(
            f"{' '.join(command)} produced more than {MAX_CAPTURE_BYTES} bytes; "
            "an incomplete transcript cannot be validated"
        )
    result = subprocess.CompletedProcess(command, completed.returncode, stdout, stderr)
    if context.check and result.returncode != 0:
        pytest.fail(
            f"{' '.join(command)} exited {result.returncode}\n"
            f"stdout:\n{result.stdout[-4000:]}\n"
            f"stderr:\n{result.stderr[-4000:]}"
        )
    return result
