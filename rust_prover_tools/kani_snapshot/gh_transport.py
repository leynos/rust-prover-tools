"""Run `gh` and classify what its failure means.

Every release API read has the same two questions to answer about a failed
call: may a fresh observation still reconcile it, and does it mean the thing
does not exist. Both answers belong here, once, so a new read cannot
accidentally report a retryable server error as permanent, or turn a transient
failure into an absence that authorises creating a release.
"""

from __future__ import annotations

import typing as typ

from rust_prover_tools.commands import CommandSpec, run_command
from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_snapshot.release_model import ReleaseTransientError

if typ.TYPE_CHECKING:
    import collections.abc as cabc

__all__ = ("failed", "gh", "is_not_found")

#: Cuprum terminates a command that exceeds its timeout and reports this code.
_TIMEOUT_EXIT_CODE: typ.Final[int] = 124

#: Transport-shaped markers that make a failed call worth re-observing.
_TRANSIENT_MARKERS: typ.Final[tuple[str, ...]] = (
    "HTTP 502",
    "HTTP 503",
    "HTTP 504",
    "HTTP 500",
    "timeout",
)


def failed(exit_code: int, stderr: str) -> ProverToolError:
    """Return the error one failed `gh` call deserves.

    A timeout, a connection failure or a retryable server status may still
    reconcile on a fresh observation; anything else is permanent.

    Parameters
    ----------
    exit_code : int
        Process exit status the command reported.
    stderr : str
        Captured standard error, which carries the HTTP status `gh` saw.

    Returns
    -------
    ProverToolError
        A `ReleaseTransientError` when a fresh observation may still reconcile
        the attempt, otherwise the permanent failure.
    """
    transient = exit_code == _TIMEOUT_EXIT_CODE or any(
        marker in stderr for marker in _TRANSIENT_MARKERS
    )
    if transient:
        msg = f"transient release API failure: {stderr.strip() or exit_code}"
        return ReleaseTransientError(msg)
    return ProverToolError(
        f"release API command failed ({exit_code}): {stderr.strip()}"
    )


def gh(argv: cabc.Sequence[str]) -> str:
    """Run one `gh` command, classifying transport failures.

    Parameters
    ----------
    argv : collections.abc.Sequence of str
        Complete argument vector.

    Returns
    -------
    str
        Captured standard output.

    Raises
    ------
    ReleaseTransientError
        Raised for a timeout, a connection failure or a retryable server error,
        because a fresh observation may still reconcile the attempt.
    ProverToolError
        Raised for a permanent failure such as an authentication refusal.
    """
    result = run_command(CommandSpec(tuple(argv)))
    if result.exit_code != 0:
        raise failed(result.exit_code, result.stderr)
    return result.stdout


def is_not_found(stderr: str) -> bool:
    """Return whether a failed `gh api` call means the thing does not exist."""
    return "HTTP 404" in stderr or "not found" in stderr.lower()
