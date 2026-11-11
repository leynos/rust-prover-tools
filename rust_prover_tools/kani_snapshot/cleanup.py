"""Bounded removal of private, unreferenced snapshot cache state.

An installation may time out while a file or tree it wrote is still only its
own: a pending activation document, a partially copied launcher, or a staging
directory. Removing that state is necessary bookkeeping, but the removal itself
can stall on a hung filesystem. Because each of these names is unique to one
attempt and no reader interprets it, a removal that outlives the budget may be
abandoned and is not retried by anyone else. Reporting it as an installation
failure would be untrue, and holding the identity lock for it would exceed the
deadline, so a deferred removal is debug-logged and left to the next sweep that
recognises its name.

A staging tree must not be swept while the worker that created it can still use
it, and a timeout abandons a worker without stopping it. Age alone therefore
cannot decide liveness. The worker takes an advisory exclusive lock, held for as
long as it may touch the tree; the kernel releases that lock when the process
exits, so a killed worker leaves a claim that a sweep can take. A sweep removes
only a tree whose claim it can take, and only within its own budget slice, so a
hung sweep cannot consume the installation that triggered it.

Cleanup of anything a reader resolves, or of a shared name that a later attempt
reuses, does not belong here: those removals must complete or be reported.
"""

from __future__ import annotations

import fcntl
import functools
import logging
import os
import secrets
import threading
import time
import typing as typ
from shutil import rmtree

from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_snapshot.progress import run_within

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

    from rust_prover_tools.kani_snapshot.install_model import InstallBudget

__all__ = (
    "STAGING_GRACE_SECONDS",
    "STAGING_OWNER_NAME",
    "StagingClaim",
    "claim_staging_ownership",
    "discard_private_file",
    "discard_private_tree",
    "new_staging_directory",
    "release_staging_ownership",
    "sweep_deferred_staging",
)

_LOGGER = logging.getLogger(__name__)
_PRIVATE_DIRECTORY_MODE: typ.Final[int] = 0o700

#: A staging tree younger than this may belong to a worker the deadline
#: abandoned but did not stop, so sweeping it could corrupt that worker's copy.
#: The grace exceeds the 30-minute installation deadline, so a tree from a live
#: install is never a candidate.
STAGING_GRACE_SECONDS: typ.Final[float] = 60 * 60.0

#: The claim file a staging worker holds locked while it may use its tree.
STAGING_OWNER_NAME: typ.Final[str] = ".owner.lock"

#: Longest a deferred sweep may take. It is a fraction of the installation
#: budget: reclaiming disk must not starve the install that triggered the sweep.
_SWEEP_SECONDS: typ.Final[float] = 15.0

#: Time a deferred sweep must leave behind. The sweep runs before staging
#: starts, so giving it the whole remaining budget would leave nothing for the
#: install; with less than this plus `_SWEEP_SECONDS` left, it is skipped.
_SWEEP_RESERVE_SECONDS: typ.Final[float] = 15.0


def discard_private_file(
    path: Path,
    *,
    budget: InstallBudget | None = None,
    phase: str = "private cleanup",
) -> None:
    """Remove one file this run owns by name, bounded by `budget`.

    A missing file is not a failure: an earlier attempt or a stalled worker may
    already have removed it. Any other `OSError`, and a removal that outlives
    the budget, is recorded and deferred because the name is never reused.
    """

    def discard() -> None:
        """Unlink the private file, treating an absent file as done."""
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            _LOGGER.debug(
                "snapshot %s could not remove %s: %s",
                phase,
                path.name,
                exc,
            )

    _supervise(phase, budget, discard, path)


def discard_private_tree(
    path: Path,
    *,
    budget: InstallBudget | None = None,
    phase: str = "private cleanup",
) -> None:
    """Remove one directory tree this run owns by name, bounded by `budget`.

    The tree is private, uniquely named and referenced by no reader, so an
    abandoned removal cannot disturb a later install. Removal failures are
    already suppressed by `rmtree`; a timeout is deferred like any other
    private cleanup.
    """

    def discard() -> None:
        """Remove the private tree, tolerating a partial removal."""
        rmtree(path, ignore_errors=True)

    _supervise(phase, budget, discard, path)


def new_staging_directory(identity_dir: Path, budget: InstallBudget) -> Path:
    """Return a fresh private staging directory beside the downloads.

    The directory is private and uniquely named, so a creation that outlives
    the budget may be abandoned: it exposes nothing, and a later install uses
    a different name.
    """
    staging = identity_dir / f".staging-{os.getpid()}-{secrets.token_hex(8)}"

    def create() -> None:
        """Create the private staging directory."""
        staging.mkdir(mode=_PRIVATE_DIRECTORY_MODE, parents=False)

    try:
        budget.supervise("staging directory creation", create)
    except OSError as exc:
        msg = f"failed to create snapshot staging directory '{staging}': {exc}"
        raise ProverToolError(msg) from exc
    return staging


def claim_staging_ownership(staging: Path) -> int:
    """Claim a staging tree for this worker and return the held descriptor.

    The caller must keep the descriptor open for as long as it may use the
    tree, and close it with `release_staging_ownership`. The claim is an
    advisory exclusive lock, so the kernel releases it when the process exits:
    a killed worker cannot leave a tree that a sweep treats as alive forever,
    and an abandoned worker that is still running keeps it claimed.
    """
    descriptor: int | None = None
    try:
        descriptor = os.open(
            staging / STAGING_OWNER_NAME,
            os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
        )
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        if descriptor is not None:
            os.close(descriptor)
        msg = f"failed to claim snapshot staging directory '{staging}': {exc}"
        raise ProverToolError(msg) from exc
    return descriptor


def release_staging_ownership(descriptor: int) -> None:
    """Release a staging claim; closing the descriptor releases its lock."""
    os.close(descriptor)


class StagingClaim:
    """An exclusive claim on a staging tree that survives abandonment.

    `run_within` abandons a stalled operation without stopping it, so a claim
    that finishes after its caller gave up must close the descriptor it
    opened. `cancel` marks the claim abandoned and closes a descriptor already
    taken; `take` closes one that arrives afterwards, so a late claim neither
    leaks a descriptor nor keeps the tree claimed forever.
    """

    def __init__(self) -> None:
        """Start an untaken, uncancelled claim."""
        self._lock = threading.Lock()
        self._descriptor: int | None = None
        self._cancelled = False

    def take(self, staging: Path) -> None:
        """Claim `staging`, or refuse if the caller already gave up."""
        descriptor = claim_staging_ownership(staging)
        with self._lock:
            if self._cancelled:
                release_staging_ownership(descriptor)
                msg = "staging ownership claim completed after the deadline"
                raise ProverToolError(msg)
            self._descriptor = descriptor

    def cancel(self) -> None:
        """Give up the claim, closing a descriptor taken before or after."""
        with self._lock:
            self._cancelled = True
            descriptor, self._descriptor = self._descriptor, None
        if descriptor is not None:
            release_staging_ownership(descriptor)

    def release(self) -> None:
        """Release a claim this worker still holds."""
        with self._lock:
            descriptor, self._descriptor = self._descriptor, None
        if descriptor is not None:
            release_staging_ownership(descriptor)


def sweep_deferred_staging(identity_dir: Path, budget: InstallBudget) -> None:
    """Remove staging trees an earlier attempt deferred and never revisited.

    A cleanup that outlives the deadline is abandoned rather than retried, and
    each attempt uses a fresh name, so without this sweep a repeated timeout
    would leave every payload it copied in the cache. The sweep gets its own
    slice of the budget, never more than `_SWEEP_SECONDS` and never the last
    `_SWEEP_RESERVE_SECONDS`, so a hung removal defers the sweep instead of
    consuming the installation that triggered it. A tree is removed only when
    its owner claim can be taken, so an abandoned worker that is still running
    keeps its copy.
    """
    allowance = min(_SWEEP_SECONDS, budget.remaining() - _SWEEP_RESERVE_SECONDS)
    if allowance <= 0.0:
        _LOGGER.debug("snapshot deferred staging sweep skipped: no budget")
        return
    try:
        run_within(
            functools.partial(_sweep_deferred_trees, identity_dir),
            remaining=lambda: allowance,
            phase="deferred staging sweep",
        )
    except ProverToolError as exc:
        _LOGGER.debug("snapshot deferred staging sweep deferred: %s", exc)


def _sweep_deferred_trees(identity_dir: Path) -> None:
    """Remove every staging tree whose owner claim this sweep can take."""
    for tree in _stale_staging_trees(identity_dir):
        discard_private_tree(tree, phase="deferred staging cleanup")


def _stale_staging_trees(identity_dir: Path) -> tuple[Path, ...]:
    """Return unclaimed staging trees old enough to have been abandoned."""
    now = time.time()
    stale: list[Path] = []
    for entry in identity_dir.glob(".staging-*"):
        try:
            age = now - entry.lstat().st_mtime
        except OSError:
            continue
        if age >= STAGING_GRACE_SECONDS and not _has_live_worker(entry):
            stale.append(entry)
    return tuple(stale)


def _has_live_worker(tree: Path) -> bool:
    """Report whether a worker still holds this staging tree's claim.

    A tree with no claim file predates the claim protocol, or was abandoned
    between its creation and its claim, so no worker of this version can be
    using it. An unreadable claim is treated as live: preserving a tree wastes
    space, while removing a live worker's copy corrupts it.
    """
    try:
        descriptor = os.open(
            tree / STAGING_OWNER_NAME,
            os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
        )
    except FileNotFoundError:
        return False
    except OSError:
        return True
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    except OSError:
        return True
    finally:
        os.close(descriptor)
    return False


def _supervise(
    phase: str,
    budget: InstallBudget | None,
    operation: cabc.Callable[[], object],
    path: Path,
) -> None:
    """Run one removal under the remaining budget, deferring on expiry."""
    try:
        if budget is None:
            operation()
        else:
            budget.supervise(phase, operation)
    except ProverToolError as exc:
        _LOGGER.debug("snapshot %s deferred for %s: %s", phase, path.name, exc)
