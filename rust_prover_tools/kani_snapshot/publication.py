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

A marker also records its base: the highest activation sequence its publication
observed when it began, read from record names only. A marker is pending until
the identity's completion watermark, which names the last installation that
finished, rises above that base. Every installation links its activation record
above all existing records before it records completion, so a completion above
a marker's base always belongs to an installation that started after the
publication did, while a late completion whose record the publication already
saw ranks no higher than the base and cannot retire it. Comparing markers
against the activation order directly cannot express that: an unrelated late
activation can outrank the record a publication observed without the
publication having finished. That distinction is what lets an abandoned
marker write land after a later installation has already reported success
without making the cache look unfinished, while an abandoned repair's marker
still refuses the cache. A marker written before markers recorded a base, or
one whose content cannot be read, names no base and is still treated as
pending.
"""

from __future__ import annotations

import dataclasses as dc
import functools
import logging
import typing as typ

from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_snapshot.completion import completion_watermark
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
    from rust_prover_tools.kani_snapshot.snapshot_layout import SnapshotLayout

__all__ = (
    "Publication",
    "marker_base_sequence",
    "prune_superseded_markers",
    "publication_pending",
    "publish_staged",
    "render_marker",
)


_PRIVATE_DIRECTORY_MODE: typ.Final[int] = 0o700
_MARKER_PREFIX: typ.Final[bytes] = b"publishing base="

_LOGGER = logging.getLogger(__name__)


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
    base_sequence : int
        Highest activation sequence observed when this publication began, or
        zero when no record existed. The marker is pending until a completion
        rises above this base.
    """

    staging: Path
    downloads: Path
    marker: Path
    stale_markers: tuple[Path, ...]
    names: tuple[str, ...]
    base_sequence: int


def render_marker(base_sequence: int) -> bytes:
    """Render the marker of a publication whose base is `base_sequence`."""
    if base_sequence < 0:
        msg = "snapshot publication base sequence must not be negative"
        raise ProverToolError(msg)
    return f"publishing base={base_sequence}\n".encode("ascii")


def marker_base_sequence(marker: Path) -> int | None:
    """Return the base a marker recorded, or `None` if unknown.

    A marker written before markers recorded a base, and one that cannot be
    read or parsed, names no base. Readers treat both as pending, because an
    unreadable marker is exactly the case in which the cache must not be
    assumed complete.
    """
    try:
        data = marker.read_bytes()
    except OSError:
        return None
    text = data.strip()
    if not text.startswith(_MARKER_PREFIX):
        return None
    digits = text.removeprefix(_MARKER_PREFIX)
    if not (digits.isascii() and digits.isdigit()):
        return None
    return int(digits)


def publication_pending(layout: SnapshotLayout) -> bool:
    """Report whether a payload publication has not been confirmed.

    The marker exists from before the first cached payload is replaced until
    the replacements and their directory flushes are durable. A cache that
    still carries a pending marker may be half-repaired, so readers must not
    validate it. Only an installation that completed above the highest
    activation the publication observed retires it, which the identity's
    completion watermark records; a marker from an older installation names
    no base and remains pending.
    """
    markers = _publication_markers(layout)
    completed = completion_watermark(layout)
    return any(_marker_is_pending(marker, completed) for marker in markers)


def _publication_markers(layout: SnapshotLayout) -> tuple[Path, ...]:
    """Return the publication markers, reporting an unreadable directory."""
    try:
        return layout.publication_markers()
    except OSError as exc:
        msg = f"failed to read snapshot publication state: {exc}"
        raise ProverToolError(msg) from exc


def _marker_is_pending(marker: Path, completed: int) -> bool:
    """Report whether `marker` still guards an unfinished publication.

    A completion retires the marker only when it rises above the marker's
    base, because every installation links its record above the activation
    records that already exist. A completion at or below that base belongs to
    an installation the publication already observed, including one whose
    watermark write was abandoned and landed late.
    """
    base = marker_base_sequence(marker)
    return base is None or base >= completed


def prune_superseded_markers(
    layout: SnapshotLayout,
    *,
    budget: InstallBudget | None = None,
) -> None:
    """Remove markers that a completed installation has superseded.

    Reuse reissues the record it validated and then records its completion above
    every marker's base, so the marker of a publication that a reuse overtook
    becomes stale without ever being observed as pending. Removal is
    best-effort: the leftover marker is no longer pending and therefore cannot
    make readers refuse the cache, so a stalled or failed removal is deferred
    rather than reported.
    """

    def prune() -> None:
        """Remove every marker the completion watermark supersedes."""
        markers = _publication_markers(layout)
        completed = completion_watermark(layout)
        for marker in markers:
            if _marker_is_pending(marker, completed):
                continue
            try:
                marker.unlink(missing_ok=True)
            except OSError as exc:
                _LOGGER.debug(
                    "snapshot marker cleanup deferred: %s", type(exc).__name__
                )

    try:
        if budget is None:
            prune()
        else:
            budget.supervise("publication marker cleanup", prune)
    except ProverToolError as exc:
        # A marker this run could not remove is harmless once a completion
        # supersedes it, so an expired budget defers it like any failure.
        _LOGGER.debug("snapshot marker cleanup deferred: %s", exc)


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
            render_marker(publication.base_sequence),
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
        """Unlink every stale marker, refusing to leave the cache marked."""
        for marker in markers:
            try:
                marker.unlink(missing_ok=True)
            except OSError as exc:
                # A marker that cannot be removed keeps `check` refusing the
                # cache, so continuing would let this installation report a
                # usable generation that readers must reject.
                msg = (
                    "snapshot cache carries a publication marker that could "
                    f"not be removed: '{marker.name}': {exc}"
                )
                raise ProverToolError(msg) from exc

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
