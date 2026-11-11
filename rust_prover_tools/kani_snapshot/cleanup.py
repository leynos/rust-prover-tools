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

Cleanup of anything a reader resolves, or of a shared name that a later attempt
reuses, does not belong here: those removals must complete or be reported.
"""

from __future__ import annotations

import functools
import logging
import shutil
import time
import typing as typ

from rust_prover_tools.errors import ProverToolError

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

    from rust_prover_tools.kani_snapshot.install_model import InstallBudget

__all__ = (
    "STAGING_GRACE_SECONDS",
    "discard_private_file",
    "discard_private_tree",
    "sweep_deferred_staging",
)

_LOGGER = logging.getLogger(__name__)

#: A staging tree younger than this may belong to a worker the deadline
#: abandoned but did not stop, so sweeping it could corrupt that worker's copy.
#: The grace exceeds the 30-minute installation deadline, so a tree from a live
#: install is never a candidate.
STAGING_GRACE_SECONDS: typ.Final[float] = 60 * 60.0


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
    already suppressed by `shutil.rmtree`; a timeout is deferred like any other
    private cleanup.
    """

    def discard() -> None:
        """Remove the private tree, tolerating a partial removal."""
        shutil.rmtree(path, ignore_errors=True)

    _supervise(phase, budget, discard, path)


def sweep_deferred_staging(identity_dir: Path, budget: InstallBudget) -> None:
    """Remove staging trees an earlier attempt deferred and never revisited.

    A cleanup that outlives the deadline is abandoned rather than retried, and
    each attempt uses a fresh name, so without this sweep a repeated timeout
    would leave every payload it copied in the cache. The sweep is bounded by
    the same budget and defers like any other private cleanup, so a slow
    filesystem cannot fail the installation that triggered it.
    """
    try:
        stale = budget.supervise(
            "deferred staging sweep",
            functools.partial(_stale_staging_trees, identity_dir),
        )
    except ProverToolError as exc:
        _LOGGER.debug("snapshot deferred staging sweep skipped: %s", exc)
        return
    for tree in stale:
        discard_private_tree(tree, budget=budget, phase="deferred staging cleanup")


def _stale_staging_trees(identity_dir: Path) -> tuple[Path, ...]:
    """Return staging trees old enough that no abandoned worker can use them."""
    now = time.time()
    stale: list[Path] = []
    for entry in identity_dir.glob(".staging-*"):
        try:
            age = now - entry.lstat().st_mtime
        except OSError:
            continue
        if age >= STAGING_GRACE_SECONDS:
            stale.append(entry)
    return tuple(stale)


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
