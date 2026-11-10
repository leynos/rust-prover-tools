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

import logging
import shutil
import typing as typ

from rust_prover_tools.errors import ProverToolError

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

    from rust_prover_tools.kani_snapshot.install_model import InstallBudget

__all__ = ("discard_private_file", "discard_private_tree")

_LOGGER = logging.getLogger(__name__)


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
