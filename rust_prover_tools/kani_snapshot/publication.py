"""Bounded publication of verified payloads into the shared cache.

Publication replaces shared files one at a time, so it cannot be abandoned
silently: a worker that timed out could still replace a file after the
installation had reported failure. The protocol here makes that possibility
visible instead of impossible. Every publication owns a uniquely named marker,
written durably before the first replacement and removed only after every
replacement and its directory flush are durable, so a cache whose publication
did not finish is recognisably incomplete. A publication that resumes after
being abandoned removes only its own marker, never a later repair's, because
no shared marker name is reused. Every blocking step is supervised, so the
caller keeps to its budget, and a replacement that outlives the budget is
reported as an unknown outcome rather than as a failure or a success.
"""

from __future__ import annotations

import dataclasses as dc
import functools
import logging
import typing as typ

from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_snapshot.durability import (
    flush_directory,
    fsync_directory,
)
from rust_prover_tools.kani_snapshot.install_generation import write_generation_file
from rust_prover_tools.kani_snapshot.progress import Progress, as_checkpoint

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

    from rust_prover_tools.kani_snapshot.install_model import InstallBudget

__all__ = ("Publication", "publish_staged")

_LOGGER = logging.getLogger(__name__)
_PRIVATE_DIRECTORY_MODE: typ.Final[int] = 0o700


@dc.dataclass(frozen=True, slots=True)
class Publication:
    """One set of verified payloads and the markers guarding publication.

    Attributes
    ----------
    staging : Path
        Private staging directory holding the verified files.
    downloads : Path
        Shared cache directory the files are published into.
    marker : Path
        Marker owned by this publication.
    stale_markers : tuple[Path, ...]
        Markers left by publications that are no longer running.
    names : tuple[str, ...]
        Payload file names, each already verified against the manifest.
    """

    staging: Path
    downloads: Path
    marker: Path
    stale_markers: tuple[Path, ...]
    names: tuple[str, ...]


def publish_staged(
    publication: Publication,
    *,
    budget: InstallBudget | None = None,
    checkpoint: Progress | None = None,
) -> None:
    """Move verified staging files into the downloads directory.

    This publication's marker is made durable before the first replacement
    and removed only after every replacement and its directory flush are
    durable, so readers never mistake a half-published cache for a complete
    one. Markers left by earlier publications are cleared only once this
    publication's marker is durable, so the incomplete state is never
    unmarked. Every blocking step is supervised; a replacement that outlives
    the budget is reported as an unknown outcome because the abandoned worker
    may still complete that atomic rename, and a later install revalidates
    the cache under the lock.
    """
    progress = as_checkpoint(checkpoint)
    try:
        progress()
        write_generation_file(
            publication.marker,
            b"publishing\n",
            "publication marker",
            budget=budget,
        )
        _clear_stale_markers(publication.stale_markers, budget)
        _supervise_publication(
            "downloads directory creation",
            budget,
            functools.partial(
                publication.downloads.mkdir,
                mode=_PRIVATE_DIRECTORY_MODE,
                parents=True,
                exist_ok=True,
            ),
        )
        for name in publication.names:
            progress()
            _replace_payload(
                publication.staging / name,
                publication.downloads / name,
                budget,
            )
        # The payload entries and the `downloads` entry itself live in
        # different directories, so both must be durable before the pointer
        # may name a generation that depends on them.
        flush_directory(
            publication.downloads,
            budget=budget,
            phase="downloads directory flush",
        )
        flush_directory(
            publication.downloads.parent,
            budget=budget,
            phase="payload directory flush",
        )
    except OSError as exc:
        msg = f"failed to publish snapshot payloads to '{publication.downloads}': {exc}"
        raise ProverToolError(msg) from exc
    _complete_publication(publication.marker, budget)


def _clear_stale_markers(
    markers: cabc.Iterable[Path],
    budget: InstallBudget | None,
) -> None:
    """Remove markers whose publications can no longer be running.

    This runs under the identity lock and only after this publication's own
    marker is durable, so the cache is never left unmarked. A later repair
    writes its own marker before clearing this one, and an abandoned worker
    can only remove the marker it owns, so clearing stale names cannot hide a
    publication that is still in progress.
    """

    def clear() -> None:
        """Unlink every stale marker."""
        for marker in markers:
            try:
                marker.unlink(missing_ok=True)
            except OSError as exc:
                _LOGGER.debug(
                    "snapshot marker cleanup skipped %s: %s",
                    marker.name,
                    exc,
                )

    _supervise_publication("publication marker cleanup", budget, clear)


def _supervise_publication(
    phase: str,
    budget: InstallBudget | None,
    operation: cabc.Callable[[], object],
) -> None:
    """Run one blocking publication step under the remaining budget."""
    if budget is None:
        operation()
    else:
        budget.supervise(phase, operation)


def _replace_verified(source: Path, target: Path) -> None:
    """Atomically move one verified payload onto its cached path."""
    source.replace(target)


def _replace_payload(source: Path, target: Path, budget: InstallBudget | None) -> None:
    """Publish one verified payload, or report that its outcome is unknown.

    A supervised replacement that times out may still complete in the
    abandoned worker. Because the payload was verified before publication,
    either outcome leaves the cache valid or detectably incomplete, but this
    run must not claim which happened.
    """
    try:
        _supervise_publication(
            "payload publication",
            budget,
            functools.partial(_replace_verified, source, target),
        )
    except ProverToolError as exc:
        msg = (
            "snapshot payload publication outcome is unknown: the deadline "
            "expired while replacing a verified payload, and a later install "
            "revalidates the cache"
        )
        raise ProverToolError(msg) from exc


def _complete_publication(marker: Path, budget: InstallBudget | None) -> None:
    """Remove this publication's marker and make its removal durable."""

    def remove_and_flush() -> None:
        """Clear this publication's marker, then flush its directory."""
        marker.unlink(missing_ok=True)
        fsync_directory(marker.parent)

    try:
        _supervise_publication("publication completion", budget, remove_and_flush)
    except OSError as exc:
        msg = f"failed to clear the snapshot publication marker: {exc}"
        raise ProverToolError(msg) from exc
    except ProverToolError as exc:
        msg = (
            "snapshot payload publication completed but its completion marker "
            f"could not be cleared: {exc}"
        )
        raise ProverToolError(msg) from exc
