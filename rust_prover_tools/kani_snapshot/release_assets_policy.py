"""Asset classification for one draft's observed state.

The policy in `release_policy` decides what a state means; this module decides
what a draft's asset set requires next. It runs from "does this draft belong to
us at all" to "what must happen next", so a conflict is never reported after an
effect that a conflicting state did not authorise. In particular, a wrong
existing asset is detected before a missing one is uploaded, and a record that
reserves a name without holding bytes is discarded rather than reported as a
conflict a retry could have repaired.

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


def _asset_map(
    assets: cabc.Iterable[ReleaseAsset] | cabc.Iterable[ObservedAsset],
) -> dict[str, ReleaseAsset | ObservedAsset]:
    """Index assets by name, refusing a repeated name."""
    indexed: dict[str, ReleaseAsset | ObservedAsset] = {}
    for asset in assets:
        if asset.name in indexed:
            msg = f"release state repeats asset {asset.name!r}"
            raise ProverToolError(msg)
        indexed[asset.name] = asset
    return indexed


def first_reason(checks: cabc.Iterable[tuple[bool, str]]) -> str | None:
    """Return the reason of the first failed check, or `None` when all pass."""
    return next((reason for failed, reason in checks if failed), None)


def unverified_message(subject: str, reason: str | None) -> str:
    """Return a refusal naming why verification failed, when the cause is known."""
    return subject if reason is None else f"{subject}: {reason}"


def _asset_problem(seen: ObservedAsset, wanted: ReleaseAsset) -> str | None:
    """Classify one asset pair as `"size"`, `"digest"`, `"unverified"` or `None`."""
    if seen.size != wanted.size:
        return "size"
    if seen.sha256 is None:
        return "unverified"
    if seen.sha256 != wanted.sha256:
        return "digest"
    return None


def _problem_names(
    wanted: dict[str, ReleaseAsset | ObservedAsset],
    seen: dict[str, ReleaseAsset | ObservedAsset],
    problem: str,
) -> list[str]:
    """Return the sorted names of present assets with `problem`."""
    return sorted(
        name
        for name, expected_asset in wanted.items()
        if name in seen
        and _asset_problem(
            typ.cast("ObservedAsset", seen[name]),
            typ.cast("ReleaseAsset", expected_asset),
        )
        == problem
    )


def _extra_asset_decision(
    wanted: dict[str, ReleaseAsset | ObservedAsset],
    seen: dict[str, ReleaseAsset | ObservedAsset],
) -> ReleaseDecision | None:
    """Refuse a draft carrying an asset the manifest does not name."""
    extra = sorted(set(seen) - set(wanted))
    if not extra:
        return None
    return ReleaseDecision(
        ReleaseAction.CONFLICT,
        f"draft carries assets outside the expected set: {', '.join(extra)}",
    )


def _placeholder_decision(
    wanted: dict[str, ReleaseAsset | ObservedAsset],
    seen: dict[str, ReleaseAsset | ObservedAsset],
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
    placeholders = tuple(
        typ.cast("ObservedAsset", seen[name])
        for name, expected_asset in sorted(wanted.items())
        if name in seen
        and typ.cast("ObservedAsset", seen[name]).is_placeholder
        and typ.cast("ReleaseAsset", expected_asset).size > 0
    )
    if not placeholders:
        return None
    return ReleaseDecision(
        ReleaseAction.DISCARD_PLACEHOLDER,
        "draft lists asset records that hold no uploaded bytes",
        discards=placeholders,
    )


def _missing_asset_decision(
    wanted: dict[str, ReleaseAsset | ObservedAsset],
    seen: dict[str, ReleaseAsset | ObservedAsset],
) -> ReleaseDecision | None:
    """Ask for an upload while a manifest asset is absent from the draft."""
    missing = [asset for name, asset in wanted.items() if name not in seen]
    if not missing:
        return None
    return ReleaseDecision(
        ReleaseAction.UPLOAD_ASSETS,
        "draft is missing expected assets",
        uploads=tuple(
            typ.cast("ReleaseAsset", asset)
            for asset in sorted(missing, key=lambda item: item.name)
        ),
    )


def _asset_mismatch_decision(
    wanted: dict[str, ReleaseAsset | ObservedAsset],
    seen: dict[str, ReleaseAsset | ObservedAsset],
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
    wanted: dict[str, ReleaseAsset | ObservedAsset],
    seen: dict[str, ReleaseAsset | ObservedAsset],
) -> ReleaseDecision | None:
    """Ask for a download while the draft's own bytes are not yet evidence."""
    unverified = _problem_names(wanted, seen, "unverified")
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

    Classifiers run from "does this draft belong to us at all" to "what must
    happen next", so a conflict is never reported after an effect that a
    conflicting state did not authorise. In particular, a wrong existing asset
    is detected before a missing one is uploaded.

    Publication additionally requires the draft's own served evidence to have
    authenticated its manifest. Verifying only at the next observation would
    report an invalid bundle after the draft had already become public, when
    its assets can no longer be replaced.
    """
    assets = observed.assets
    wanted = _asset_map(expected.assets)
    seen = _asset_map(assets)
    classifiers = (
        _extra_asset_decision,
        _placeholder_decision,
        _asset_mismatch_decision,
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
