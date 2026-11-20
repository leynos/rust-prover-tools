"""Asset classification for one draft's observed state.

The policy in `release_policy` decides what a state means; this module decides
what a draft's asset set requires next. It runs from "does this draft belong to
us at all" to "what must happen next", so a conflict is never reported after an
effect that a conflicting state did not authorise. In particular, a wrong
existing asset is detected before a missing one is uploaded, and a record that
reserves a name without holding bytes is discarded rather than reported as a
conflict a retry could have repaired — but only when nothing else in the draft
already disagrees, so a refusal never edits the draft it refused, and only
after any asset whose bytes were never hashed has been downloaded and shown to
match.

Nothing here performs an effect or reads the API: the functions are total over
an observation and return the next `ReleaseDecision`.
"""

from __future__ import annotations

import typing as typ

from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_snapshot.release_model import (
    ExpectedRelease,
    ObservedAsset,
    ObservedRelease,
    ReleaseAction,
    ReleaseAsset,
    ReleaseDecision,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

__all__ = ("classify_assets", "first_reason", "unverified_message")


def _asset_map[AssetT: ReleaseAsset | ObservedAsset](
    assets: cabc.Iterable[AssetT],
) -> dict[str, AssetT]:
    """Index assets by name, refusing a repeated name.

    Either side of a comparison may be indexed, but never both under one
    mapping: the two carry different fields, and only an observed asset
    reports a placeholder or a digest.
    """
    indexed: dict[str, AssetT] = {}
    for asset in assets:
        if asset.name in indexed:
            msg = f"release state repeats asset {asset.name!r}"
            raise ProverToolError(msg)
        indexed[asset.name] = asset
    return indexed


def first_reason(checks: cabc.Iterable[tuple[bool, str]]) -> str | None:
    """Return the reason of the first failed check, or `None` when all pass.

    Parameters
    ----------
    checks : collections.abc.Iterable of tuple[bool, str]
        Checks in priority order, each pairing a failed condition with the
        refusal that condition earns.

    Returns
    -------
    str | None
        The reason of the first check whose condition holds, or `None` when
        every check passes.
    """
    return next((reason for failed, reason in checks if failed), None)


def unverified_message(subject: str, reason: str | None) -> str:
    """Return a refusal naming why verification failed, when the cause is known.

    Parameters
    ----------
    subject : str
        The refusal to report.
    reason : str | None
        The cause the verification reported, or `None` when it reported none.

    Returns
    -------
    str
        `subject` alone for a `None` reason, otherwise `subject` with the
        reason appended after a colon.
    """
    return subject if reason is None else f"{subject}: {reason}"


def _is_placeholder_pair(seen: ObservedAsset, wanted: ReleaseAsset) -> bool:
    """Return whether a record reserves an expected, non-empty asset's name.

    Such a record holds no bytes at all: an upload interrupted after GitHub
    created the row leaves it behind. It is not an asset that disagrees with
    the manifest, so it is neither a mismatch nor an unverified copy of one;
    the classifier that runs before an upload decides whether to delete it.
    """
    return seen.is_placeholder and wanted.size > 0


def _asset_problem(seen: ObservedAsset, wanted: ReleaseAsset) -> str | None:
    """Classify one asset pair as `"size"`, `"digest"`, `"unverified"` or `None`."""
    if _is_placeholder_pair(seen, wanted):
        return None
    if seen.size != wanted.size:
        return "size"
    if seen.sha256 is None:
        return "unverified"
    if seen.sha256 != wanted.sha256:
        return "digest"
    return None


def _problem_names(
    wanted: cabc.Mapping[str, ReleaseAsset],
    seen: cabc.Mapping[str, ObservedAsset],
    problem: str,
) -> list[str]:
    """Return the sorted names of present assets with `problem`."""
    return sorted(
        name
        for name, expected_asset in wanted.items()
        if name in seen and _asset_problem(seen[name], expected_asset) == problem
    )


def _extra_asset_decision(
    wanted: cabc.Mapping[str, ReleaseAsset],
    seen: cabc.Mapping[str, ObservedAsset],
) -> ReleaseDecision | None:
    """Refuse a draft carrying an asset the manifest does not name."""
    extra = sorted(set(seen) - set(wanted))
    if not extra:
        return None
    return ReleaseDecision(
        ReleaseAction.CONFLICT,
        f"draft carries assets outside the expected set: {', '.join(extra)}",
    )


def _placeholder_pairs(
    wanted: cabc.Mapping[str, ReleaseAsset],
    seen: cabc.Mapping[str, ObservedAsset],
) -> tuple[ObservedAsset, ...]:
    """Return the records that reserve an expected name but hold no bytes."""
    return tuple(
        seen[name]
        for name, expected_asset in sorted(wanted.items())
        if name in seen and _is_placeholder_pair(seen[name], expected_asset)
    )


def _placeholder_decision(
    wanted: cabc.Mapping[str, ReleaseAsset],
    seen: cabc.Mapping[str, ObservedAsset],
) -> ReleaseDecision | None:
    """Delete a record that reserves an expected name but holds no bytes.

    An upload interrupted after GitHub created the asset record leaves an
    empty record behind. It is not an asset: no bytes can verify it, and an
    upload refuses to replace an existing name, so without this step the run
    would report a conflict that a retry could have repaired. Deleting the
    record is recoverable in both directions: the next observation either sees
    the name absent and uploads it, or still sees the placeholder and discards
    it again.
    """
    placeholders = _placeholder_pairs(wanted, seen)
    if not placeholders:
        return None
    return ReleaseDecision(
        ReleaseAction.DISCARD_PLACEHOLDER,
        "draft lists asset records that hold no uploaded bytes",
        discards=placeholders,
    )


def _draft_needs_edit(
    wanted: cabc.Mapping[str, ReleaseAsset],
    seen: cabc.Mapping[str, ObservedAsset],
) -> bool:
    """Return whether repairing the draft would delete or upload anything."""
    return bool(_placeholder_pairs(wanted, seen)) or any(
        name not in seen for name in wanted
    )


def _present_unverified(
    wanted: cabc.Mapping[str, ReleaseAsset],
    seen: cabc.Mapping[str, ObservedAsset],
) -> tuple[str, ...]:
    """Return the present, non-placeholder assets whose bytes are unknown."""
    return tuple(_problem_names(wanted, seen, "unverified"))


def _hash_before_edit_decision(
    wanted: cabc.Mapping[str, ReleaseAsset],
    seen: cabc.Mapping[str, ObservedAsset],
) -> ReleaseDecision | None:
    """Download the unknown bytes before a draft that needs an edit is edited.

    A listed size is a claim, and an absent digest only means nobody has
    hashed the bytes yet. Deleting a placeholder or uploading a missing file
    on that basis would edit a draft that may turn out to hold different
    bytes of the same size, so the refusal would be reported after effects its
    state did not authorise. When the draft needs an edit and some present asset
    has no observed digest, those assets are downloaded and hashed first;
    the next pass then either refuses the draft as a conflict or performs the
    edit on bytes it knows match the manifest.

    Only a draft that needs no edit reaches the later full verification
    instead: its complete asset set is downloaded as one authenticated bundle,
    and that download already hashes every asset.
    """
    if not _draft_needs_edit(wanted, seen):
        return None
    unverified = _present_unverified(wanted, seen)
    if not unverified:
        return None
    return ReleaseDecision(
        ReleaseAction.HASH_ASSETS,
        "draft assets must be hashed before the draft is edited",
        verifications=unverified,
    )


def _missing_asset_decision(
    wanted: cabc.Mapping[str, ReleaseAsset],
    seen: cabc.Mapping[str, ObservedAsset],
) -> ReleaseDecision | None:
    """Ask for an upload while a manifest asset is absent from the draft."""
    missing = [asset for name, asset in wanted.items() if name not in seen]
    if not missing:
        return None
    return ReleaseDecision(
        ReleaseAction.UPLOAD_ASSETS,
        "draft is missing expected assets",
        uploads=tuple(sorted(missing, key=lambda item: item.name)),
    )


def _asset_mismatch_decision(
    wanted: cabc.Mapping[str, ReleaseAsset],
    seen: cabc.Mapping[str, ObservedAsset],
) -> ReleaseDecision | None:
    """Refuse assets whose size or observed digest differs from the manifest."""
    mismatched = sorted(
        _problem_names(wanted, seen, "size") + _problem_names(wanted, seen, "digest")
    )
    if not mismatched:
        return None
    return ReleaseDecision(
        ReleaseAction.CONFLICT,
        f"draft assets differ from the expected bytes: {', '.join(mismatched)}",
    )


def _unverified_asset_decision(
    wanted: cabc.Mapping[str, ReleaseAsset],
    seen: cabc.Mapping[str, ObservedAsset],
) -> ReleaseDecision | None:
    """Ask for a download while the draft's own bytes are not yet evidence."""
    unverified = _present_unverified(wanted, seen)
    if not unverified:
        return None
    return ReleaseDecision(
        ReleaseAction.VERIFY_ASSETS,
        "draft assets have not been re-downloaded and hashed",
        verifications=tuple(unverified),
    )


def classify_assets(
    expected: ExpectedRelease,
    observed: ObservedRelease,
) -> ReleaseDecision:
    """Decide what a draft's current asset set requires next.

    The caller passes an existing draft whose identity, target and origin it
    has already checked; this function judges only the assets.

    Classifiers run from "does this draft belong to us at all" to "what must
    happen next", so a conflict is never reported after an effect that a
    conflicting state did not authorise. In particular, a wrong existing asset
    is detected before a missing one is uploaded, and before a placeholder is
    discarded: a state that already conflicts must leave the draft exactly as
    the run found it, so the bytes someone can inspect are the bytes that
    caused the refusal.

    An asset whose bytes have not been hashed is not yet known to be sound,
    so a draft that needs an edit is edited only after those bytes are
    downloaded and hashed: a size that happens to match is not evidence that
    the bytes do. The edit itself (deleting a placeholder, uploading a missing
    file) follows the hash, and the next observation sees the asset as
    verified against the bytes that were actually served.

    Publication additionally requires the draft's own served evidence to have
    authenticated its manifest. Verifying only at the next observation would
    report an invalid bundle after the draft had already become public, when
    its assets can no longer be replaced.

    Parameters
    ----------
    expected : ExpectedRelease
        Identity and bytes the draft must carry.
    observed : ObservedRelease
        Existing draft, including the assets and digests it reports.

    Returns
    -------
    ReleaseDecision
        The next action for this draft and the reason for it.
    """
    assets = observed.assets
    wanted = _asset_map(expected.assets)
    seen = _asset_map(assets)
    classifiers = (
        _extra_asset_decision,
        _asset_mismatch_decision,
        _hash_before_edit_decision,
        _placeholder_decision,
        _missing_asset_decision,
        _unverified_asset_decision,
    )
    for classifier in classifiers:
        decision = classifier(wanted, seen)
        if decision is not None:
            return decision
    if observed.provenance_verified:
        return ReleaseDecision(
            ReleaseAction.PUBLISH,
            "draft is complete, verified and bound to the validated run",
        )
    if observed.provenance_reason is None:
        # A complete draft whose bytes have not been downloaded yet is not a
        # conflict: the query that produced this observation cannot
        # authenticate anything, so verification is the next step.
        return ReleaseDecision(
            ReleaseAction.VERIFY_ASSETS,
            "draft's served evidence has not been authenticated",
            verifications=tuple(sorted(asset.name for asset in assets)),
        )
    return ReleaseDecision(
        ReleaseAction.CONFLICT,
        unverified_message(
            "draft's served evidence did not authenticate its manifest",
            observed.provenance_reason,
        ),
    )
