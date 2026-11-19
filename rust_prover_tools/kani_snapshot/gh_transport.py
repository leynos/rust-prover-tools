"""Run `gh` and classify what its failure means.

Every release API read has the same two questions to answer about a failed
call: may a fresh observation still reconcile it, and does it mean the thing
does not exist. Both answers belong here, once, so a new read cannot
accidentally report a retryable server error as permanent, or turn a transient
failure into an absence that authorises creating a release.

The wrapper also owns the wall-clock budget. An unbounded `gh` call could
outlive the run that started it, while a timed-out write may still have landed,
so a timeout is classified as transient rather than as a definite failure.
"""

from __future__ import annotations

import logging
import typing as typ

from rust_prover_tools.commands import (
    CommandResult,
    CommandSpec,
    CommandTimeoutError,
    run_command,
)
from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_snapshot.release_model import ReleaseTransientError
from rust_prover_tools.observability import log_event

if typ.TYPE_CHECKING:
    import collections.abc as cabc

__all__ = (
    "READ_TIMEOUT_SECONDS",
    "TRANSFER_TIMEOUT_SECONDS",
    "GhRunner",
    "failed",
    "gh",
    "gh_optional",
    "is_not_found",
)

_LOGGER = logging.getLogger(__name__)

#: Cuprum terminates a command that exceeds its timeout and reports this code.
_TIMEOUT_EXIT_CODE: typ.Final[int] = 124

#: Budget for a metadata read such as an API lookup or a release listing.
READ_TIMEOUT_SECONDS: typ.Final[float] = 120.0

#: Budget for an asset transfer, which moves far more bytes than a read.
TRANSFER_TIMEOUT_SECONDS: typ.Final[float] = 300.0

#: Failures that describe a refused identity or missing permission. These stay
#: permanent even when the same message contains a word that usually means
#: "retry", because the same credentials cannot succeed later.
_PERMANENT_MARKERS: typ.Final[tuple[str, ...]] = (
    "HTTP 401",
    "HTTP 403",
    "authentication",
    "permission",
)

#: Transport-shaped markers that make a failed call worth re-observing.
_TRANSIENT_MARKERS: typ.Final[tuple[str, ...]] = (
    "HTTP 500",
    "HTTP 502",
    "HTTP 503",
    "HTTP 504",
    "timeout",
    "connection reset",
    "connection refused",
    "network is unreachable",
    "TLS handshake timeout",
    "unexpected EOF",
    "i/o timeout",
)


class GhRunner(typ.Protocol):
    """One complete `gh` invocation, returning its captured standard output.

    Collectors depend on this port rather than calling the process runner
    themselves, so the exact argument vector they build can be inspected
    without starting a process. `gh` is the production implementation; a test
    supplies a recorder and reads back what the collector asked for.
    """

    def __call__(
        self,
        argv: cabc.Sequence[str],
        *,
        timeout_seconds: float | None = None,
    ) -> str:
        """Run one `gh` command and return its standard output."""
        ...


def _contains_marker(stderr: str, markers: tuple[str, ...]) -> bool:
    """Return whether `stderr` contains any of `markers`, ignoring case."""
    lowered = stderr.lower()
    return any(marker.lower() in lowered for marker in markers)


def failed(exit_code: int, stderr: str) -> ProverToolError:
    """Return the error one failed `gh` call deserves.

    A timeout, a connection failure or a retryable server status may still
    reconcile on a fresh observation; anything else is permanent. An explicit
    authentication or permission refusal is permanent even if its text also
    mentions a transient word, because retrying cannot change the answer.

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
    if _contains_marker(stderr, _PERMANENT_MARKERS):
        return ProverToolError(
            f"release API command failed ({exit_code}): {stderr.strip()}"
        )
    transient = exit_code == _TIMEOUT_EXIT_CODE or _contains_marker(
        stderr, _TRANSIENT_MARKERS
    )
    if transient:
        msg = f"transient release API failure: {stderr.strip() or exit_code}"
        return ReleaseTransientError(msg)
    return ProverToolError(
        f"release API command failed ({exit_code}): {stderr.strip()}"
    )


def _kind(error: ProverToolError) -> str:
    """Return the bounded classification a collector may label a failure with."""
    return "transient" if isinstance(error, ReleaseTransientError) else "permanent"


def _raise_classified(result: CommandResult) -> typ.NoReturn:
    """Raise the error one failed call deserves, recording its category.

    The event carries the exit status and the classification only: the failure
    text can embed a URL or a server message and is kept out of the event so a
    collector never aggregates unbounded strings.
    """
    error = failed(result.exit_code, result.stderr)
    log_event(
        _LOGGER,
        "kani.snapshot.gh.failed",
        exit_code=result.exit_code,
        kind=_kind(error),
    )
    raise error


def _run(
    argv: cabc.Sequence[str],
    timeout_seconds: float | None,
) -> CommandResult:
    """Run one `gh` invocation under a resolved wall-clock budget.

    A timed-out command becomes a transient failure here, once, so every
    caller inherits the rule that a write which may still have landed must be
    re-observed rather than reported as failed.
    """
    budget = READ_TIMEOUT_SECONDS if timeout_seconds is None else timeout_seconds
    try:
        return run_command(CommandSpec(tuple(argv), timeout_seconds=budget))
    except CommandTimeoutError as exc:
        log_event(_LOGGER, "kani.snapshot.gh.failed", kind="timeout")
        msg = f"transient release API failure: {exc}"
        raise ReleaseTransientError(msg) from exc


def gh(
    argv: cabc.Sequence[str],
    *,
    timeout_seconds: float | None = None,
) -> str:
    """Run one `gh` command, classifying transport failures.

    Parameters
    ----------
    argv : collections.abc.Sequence of str
        Complete argument vector.
    timeout_seconds : float | None
        Wall-clock budget for the call. `None` uses the read budget; an asset
        transfer should pass `TRANSFER_TIMEOUT_SECONDS`.

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
    result = _run(argv, timeout_seconds)
    if result.exit_code != 0:
        _raise_classified(result)
    return result.stdout


def gh_optional(
    argv: cabc.Sequence[str],
    *,
    timeout_seconds: float | None = None,
) -> str | None:
    """Run one `gh` read whose subject may legitimately be absent.

    This is the absent-tolerant form of `gh`, sharing its budget and
    classification. It returns `None` only for a confirmed HTTP 404, so a
    retryable server error that happens to say "not found" is still raised
    rather than mistaken for absence.

    Parameters
    ----------
    argv : collections.abc.Sequence of str
        Complete argument vector.
    timeout_seconds : float | None
        Wall-clock budget for the call. `None` uses the read budget.

    Returns
    -------
    str | None
        Captured standard output, or `None` when the API confirms absence.

    Raises
    ------
    ReleaseTransientError
        Raised for a timeout, a connection failure or a retryable server error.
    ProverToolError
        Raised for any other failure, including an authentication refusal.
    """
    result = _run(argv, timeout_seconds)
    if result.exit_code == 0:
        return result.stdout
    if is_not_found(result.stderr):
        log_event(_LOGGER, "kani.snapshot.gh.absent", exit_code=result.exit_code)
        return None
    _raise_classified(result)


def is_not_found(stderr: str) -> bool:
    """Return whether a failed `gh api` call confirmed the thing is absent.

    Only an HTTP 404 means the subject does not exist. Wording such as
    "not found" on a 503 describes an unavailable service, not an absence, and
    authorising a create from it could collide with an existing release.
    """
    return "http 404" in stderr.lower()
