"""Deciding whether two readings of a release describe the same state.

A publication step reads the release, then does something that takes time — a
download, a verification, a write — and then has to decide what it may believe.
The answer this module owns is narrow and used at every such boundary: which
parts of a reading describe the release, so that a reading taken across a
change can be told apart from one taken of the same state.

Keeping it apart from the orchestration lets `publish` spend its budget on the
state machine, and keeps the comparison testable on its own.
"""

from __future__ import annotations

import typing as typ

from rust_prover_tools.kani_snapshot.release_model import (
    ObservedRelease,
    ReleaseTransientError,
)

if typ.TYPE_CHECKING:
    from rust_prover_tools.kani_snapshot.release_port import ReleaseApi

__all__ = ("observe_or_keep", "settle", "state_key")


def state_key(observed: ObservedRelease) -> tuple[object, ...]:
    """Return the parts of an observation the policy decides from.

    Every field the policy reads is included, not only the asset set: a draft
    can be retargeted or re-bodied while its bytes are being downloaded, and
    the newer reading is the one that would be refused. Only the computed
    digests are left out, because the two readings sit on either side of a
    download, so one of them carries hashes the other cannot, and that
    difference alone is not a change of state.
    """
    return (
        observed.exists,
        observed.is_draft,
        observed.release_id,
        observed.tag,
        observed.target_commitish,
        observed.tag_commit,
        observed.body,
        tuple(
            sorted(
                (asset.name, asset.size, asset.asset_id) for asset in observed.assets
            )
        ),
    )


def settle(
    api: ReleaseApi,
    tag: str,
    observed: ObservedRelease,
    completed: ObservedRelease,
) -> ObservedRelease:
    """Return the reading a completed download may be decided from.

    A download takes time, and its listing may have gone out of date by the
    time it finishes: an asset can be removed after its own bytes were read, a
    draft retargeted, a release's notes rewritten. The completed reading only
    describes the release the policy saw if the release still matches it, so
    the state is read once more and the completed reading is discarded when it
    does not. The newer reading then carries no verification verdict, and the
    next decision asks for the download it needs rather than trusting one taken
    across a change.
    """
    confirmed = api.observe(tag)
    if state_key(confirmed) != state_key(observed):
        return confirmed
    return completed


def observe_or_keep(
    api: ReleaseApi,
    tag: str,
    observed: ObservedRelease | None,
) -> ObservedRelease | None:
    """Observe the release once, keeping the previous state after a retryable error."""
    if observed is not None:
        return observed
    try:
        return api.observe(tag)
    except ReleaseTransientError:
        return None
