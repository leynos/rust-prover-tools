"""Pure release-state policy for immutable snapshot publication.

Publication is a state machine over one immutable tag, never a sequence of
unchecked writes. The states that matter are absent, a draft release, a draft
whose assets are incomplete or unverified, a published release that matches the
expected identity, and a conflict that must be diagnosed rather than repaired
in place. The state shapes live in `release_model`; this module is the only
place that interprets them.

The lemma this module exists for is narrow and testable: `decide` never returns
`PUBLISH` while an expected asset is absent, has the wrong size, or has a
digest that has not been observed; it never returns `NOOP` for a published
release whose bytes differ; and it never returns an action that would delete,
replace or move anything once a release is published. Recovery from an
interrupted attempt is expressed by re-observing state, not by repeating a
write, so a publication that timed out after succeeding is recognised as a
no-op on the next observation.
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
    ReleaseTransientError,
    ValidationBinding,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

__all__ = (
    "ExpectedRelease",
    "ObservedAsset",
    "ObservedRelease",
    "ReleaseAction",
    "ReleaseAsset",
    "ReleaseDecision",
    "ReleaseTransientError",
    "ValidationBinding",
    "decide",
)


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


def _binding_conflict(
    expected: ExpectedRelease,
    validation: ValidationBinding,
) -> str | None:
    """Return why validation evidence does not bind, or `None` when it does."""
    if not validation.verification_passed:
        return "clean validation did not pass"
    if (validation.run_id, validation.run_attempt) != (
        expected.run_id,
        expected.run_attempt,
    ):
        return "validation receipt belongs to another workflow run or attempt"
    if validation.manifest_sha256 != expected.manifest_sha256:
        return "validation receipt names another manifest digest"
    expected_names = {asset.name for asset in expected.assets}
    if set(validation.assets) != expected_names:
        return "validation receipt records an incomplete asset set"
    return None


def _published_conflict(
    expected: ExpectedRelease,
    observed: ObservedRelease,
) -> str | None:
    """Return why a published release differs from the expected identity."""
    if observed.tag != expected.tag:
        return "published release reports another tag"
    if observed.target_commitish not in {None, expected.builder_commit}:
        return "published release names another builder commit"
    return None


def _asset_problems(
    wanted: dict[str, ReleaseAsset | ObservedAsset],
    seen: dict[str, ReleaseAsset | ObservedAsset],
) -> tuple[list[str], list[str]]:
    """Return the mismatched and not-yet-hashed asset names, both sorted."""
    mismatched: list[str] = []
    unverified: list[str] = []
    for name, expected_asset in wanted.items():
        seen_asset = typ.cast("ObservedAsset", seen[name])
        wanted_asset = typ.cast("ReleaseAsset", expected_asset)
        if seen_asset.size != wanted_asset.size:
            mismatched.append(name)
        elif seen_asset.sha256 is None:
            unverified.append(name)
        elif seen_asset.sha256 != wanted_asset.sha256:
            mismatched.append(name)
    return sorted(mismatched), sorted(unverified)


def _classify_assets(
    expected: ExpectedRelease,
    observed: tuple[ObservedAsset, ...],
) -> ReleaseDecision:
    """Decide what a draft's current asset set requires next."""
    wanted = _asset_map(expected.assets)
    seen = _asset_map(observed)
    extra = sorted(set(seen) - set(wanted))
    if extra:
        return ReleaseDecision(
            ReleaseAction.CONFLICT,
            f"draft carries assets outside the expected set: {', '.join(extra)}",
        )
    missing = [asset for name, asset in wanted.items() if name not in seen]
    if missing:
        return ReleaseDecision(
            ReleaseAction.UPLOAD_ASSETS,
            "draft is missing expected assets",
            uploads=tuple(
                typ.cast("ReleaseAsset", asset)
                for asset in sorted(missing, key=lambda item: item.name)
            ),
        )
    mismatched, unverified = _asset_problems(wanted, seen)
    if mismatched:
        return ReleaseDecision(
            ReleaseAction.CONFLICT,
            f"draft assets differ from the expected bytes: {', '.join(mismatched)}",
        )
    if unverified:
        return ReleaseDecision(
            ReleaseAction.VERIFY_ASSETS,
            "draft assets have not been re-downloaded and hashed",
            verifications=tuple(unverified),
        )
    return ReleaseDecision(
        ReleaseAction.PUBLISH,
        "draft is complete, verified and bound to the validated run",
    )


def _classify_draft(
    expected: ExpectedRelease,
    observed: ObservedRelease,
) -> ReleaseDecision:
    """Refuse a draft whose identity differs, else classify its assets."""
    if observed.tag != expected.tag:
        return ReleaseDecision(
            ReleaseAction.CONFLICT,
            "draft reports another tag",
        )
    if observed.target_commitish not in {None, expected.builder_commit}:
        return ReleaseDecision(
            ReleaseAction.CONFLICT,
            "draft targets another builder commit",
        )
    return _classify_assets(expected, observed.assets)


def _classify_published(
    expected: ExpectedRelease,
    observed: tuple[ObservedAsset, ...],
) -> ReleaseDecision:
    """Treat a published release as a no-op only when its bytes match exactly."""
    wanted = _asset_map(expected.assets)
    seen = _asset_map(observed)
    if set(seen) != set(wanted):
        return ReleaseDecision(
            ReleaseAction.CONFLICT,
            "published release has a different asset set",
        )
    unverified = [name for name, asset in seen.items() if asset.sha256 is None]
    if unverified:
        return ReleaseDecision(
            ReleaseAction.VERIFY_ASSETS,
            "published assets have not been re-downloaded and hashed",
            verifications=tuple(sorted(unverified)),
        )
    for name, expected_asset in wanted.items():
        seen_asset = typ.cast("ObservedAsset", seen[name])
        if (
            seen_asset.size != expected_asset.size
            or seen_asset.sha256 != expected_asset.sha256
        ):
            return ReleaseDecision(
                ReleaseAction.CONFLICT,
                f"published asset {name!r} differs from the expected bytes",
            )
    return ReleaseDecision(
        ReleaseAction.NOOP,
        "published release already matches the expected identity",
    )


def decide(
    expected: ExpectedRelease,
    observed: ObservedRelease,
    validation: ValidationBinding,
) -> ReleaseDecision:
    """Return the next publication action for one observed release state.

    Parameters
    ----------
    expected : ExpectedRelease
        Immutable identity the attempt must publish.
    observed : ObservedRelease
        Current API state, including digests of any assets that exist.
    validation : ValidationBinding
        Evidence from the same run and attempt that clean validation passed.

    Returns
    -------
    ReleaseDecision
        The next action and its reason.
    """
    conflict = _binding_conflict(expected, validation)
    if conflict is not None:
        return ReleaseDecision(ReleaseAction.CONFLICT, conflict)
    if not observed.exists:
        return ReleaseDecision(
            ReleaseAction.CREATE_DRAFT,
            "no release exists for the immutable tag",
        )
    if observed.is_draft:
        return _classify_draft(expected, observed)
    published_conflict = _published_conflict(expected, observed)
    if published_conflict is not None:
        return ReleaseDecision(ReleaseAction.CONFLICT, published_conflict)
    return _classify_published(expected, observed.assets)
