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
release whose recorded inputs differ or whose own publication did not verify;
and it never returns an action that would delete, replace or move anything
once a release is published. Recovery from an interrupted attempt is expressed
by re-observing state, not by repeating a write, so a publication that timed
out after succeeding is recognised as a no-op on the next observation.

A published release is compared by its recorded build-input fingerprint, not
by the new run's asset bytes. A rebuild of unchanged inputs still produces a
new manifest — it records a new run, attempt, cache outcome and timings — so
byte equality could never hold. The published release's own bytes are instead
verified against its own manifest, which the adapter reports as
`ObservedRelease.provenance_verified`.
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
    parse_input_marker,
    parse_run_marker,
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
    checks = (
        (
            not validation.verification_passed,
            "clean validation did not pass",
        ),
        (
            (validation.run_id, validation.run_attempt)
            != (expected.run_id, expected.run_attempt),
            "validation receipt belongs to another workflow run or attempt",
        ),
        (
            validation.manifest_sha256 != expected.manifest_sha256,
            "validation receipt names another manifest digest",
        ),
        (
            set(validation.assets) != {asset.name for asset in expected.assets},
            "validation receipt records an incomplete asset set",
        ),
    )
    return _first_reason(checks)


def _first_reason(checks: cabc.Iterable[tuple[bool, str]]) -> str | None:
    """Return the reason of the first failed check, or `None` when all pass."""
    return next((reason for failed, reason in checks if failed), None)


def _published_conflict(
    expected: ExpectedRelease,
    observed: ObservedRelease,
) -> str | None:
    """Return why a published release differs from the expected identity."""
    return _first_reason((
        (
            observed.tag != expected.tag,
            "published release reports another tag",
        ),
        (
            observed.target_commitish not in {None, expected.builder_commit},
            "published release names another builder commit",
        ),
    ))


def _asset_problem(seen: ObservedAsset, wanted: ReleaseAsset) -> str | None:
    """Classify one asset pair as `"mismatch"`, `"unverified"` or `None`."""
    if seen.size != wanted.size:
        return "mismatch"
    if seen.sha256 is None:
        return "unverified"
    if seen.sha256 != wanted.sha256:
        return "mismatch"
    return None


def _asset_problems(
    wanted: dict[str, ReleaseAsset | ObservedAsset],
    seen: dict[str, ReleaseAsset | ObservedAsset],
) -> tuple[list[str], list[str]]:
    """Return the mismatched and not-yet-hashed asset names, both sorted."""
    problems = {
        name: _asset_problem(
            typ.cast("ObservedAsset", seen[name]),
            typ.cast("ReleaseAsset", expected_asset),
        )
        for name, expected_asset in wanted.items()
    }
    mismatched = sorted(
        name for name, problem in problems.items() if problem == "mismatch"
    )
    unverified = sorted(
        name for name, problem in problems.items() if problem == "unverified"
    )
    return mismatched, unverified


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


def _asset_problem_decision(
    wanted: dict[str, ReleaseAsset | ObservedAsset],
    seen: dict[str, ReleaseAsset | ObservedAsset],
) -> ReleaseDecision | None:
    """Refuse differing bytes, or ask for a verification while digests are absent.

    Only assets the draft already carries are compared. A present asset with
    other bytes already proves the draft cannot be resumed, so that conflict
    must be found before an upload for an absent asset is requested; otherwise
    a draft that is both incomplete and wrong would be written to first and
    diagnosed afterwards.
    """
    present = {name: asset for name, asset in wanted.items() if name in seen}
    mismatched, unverified = _asset_problems(present, seen)
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
    return None


def _classify_assets(
    expected: ExpectedRelease,
    observed: tuple[ObservedAsset, ...],
) -> ReleaseDecision:
    """Decide what a draft's current asset set requires next.

    Classifiers run from "does this draft belong to us at all" to "what must
    happen next", so a conflict is never reported after an effect that a
    conflicting state did not authorise. In particular, a wrong existing asset
    is detected before a missing one is uploaded.
    """
    wanted = _asset_map(expected.assets)
    seen = _asset_map(observed)
    classifiers = (
        _extra_asset_decision,
        _asset_problem_decision,
        _missing_asset_decision,
    )
    for classifier in classifiers:
        decision = classifier(wanted, seen)
        if decision is not None:
            return decision
    return ReleaseDecision(
        ReleaseAction.PUBLISH,
        "draft is complete, verified and bound to the validated run",
    )


def _draft_binding_conflict(
    expected: ExpectedRelease,
    observed: ObservedRelease,
) -> str | None:
    """Return why a draft does not belong to this run, or `None` when it does."""
    run = parse_run_marker(observed.body)
    fingerprint = parse_input_marker(observed.body)
    return _first_reason((
        (
            run is None,
            "draft does not record the run and attempt that created it",
        ),
        (
            run != (expected.run_id, expected.run_attempt),
            "draft was created by another workflow run or attempt",
        ),
        (
            fingerprint is None,
            "draft does not record its build inputs",
        ),
        (
            fingerprint != expected.input_fingerprint,
            "draft was created from different build inputs",
        ),
    ))


def _classify_draft(
    expected: ExpectedRelease,
    observed: ObservedRelease,
) -> ReleaseDecision:
    """Refuse a draft whose origin differs, else classify its assets."""
    conflict = _draft_binding_conflict(expected, observed)
    if conflict is not None:
        return ReleaseDecision(ReleaseAction.CONFLICT, conflict)
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
    observed: ObservedRelease,
) -> ReleaseDecision:
    """Treat a published release as a no-op only when it verifies.

    The expected asset names are compared because they are derived from the
    same inputs, but their bytes are not: this run assembled a new manifest,
    so only the published release's own evidence can authenticate its bytes.
    """
    fingerprint = parse_input_marker(observed.body)
    conflict = _first_reason((
        (
            fingerprint is None,
            "published release does not record its build inputs",
        ),
        (
            fingerprint != expected.input_fingerprint,
            "published release records different build inputs",
        ),
        (
            {asset.name for asset in observed.assets}
            != {asset.name for asset in expected.assets},
            "published release has a different asset set",
        ),
        (
            not observed.provenance_verified,
            "published release did not authenticate its own publication",
        ),
    ))
    if conflict is not None:
        return ReleaseDecision(ReleaseAction.CONFLICT, conflict)
    return ReleaseDecision(
        ReleaseAction.NOOP,
        "published release records the same inputs and verifies against its manifest",
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
    return _classify_published(expected, observed)
