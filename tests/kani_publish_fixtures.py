"""Shared fixtures for the immutable snapshot publication tests.

The publication tests all describe one exact release: four payloads with known
bytes, one builder commit and one validated run. Keeping those values here means
a policy witness, an orchestration test and a generated state machine cannot
drift into describing different releases, which would make a passing test prove
nothing about the others.

The release API double lives in `tests.kani_release_api_fixtures`, keeping its
mutable server state and failure schedule away from these shared values.
"""

from __future__ import annotations

import hashlib
import json
import typing as typ

from rust_prover_tools.kani_snapshot.publish import PublicationRequest
from rust_prover_tools.kani_snapshot.release_model import release_body
from rust_prover_tools.kani_snapshot.release_policy import (
    ExpectedRelease,
    ObservedAsset,
    ObservedRelease,
    ReleaseAsset,
    ValidationBinding,
)

if typ.TYPE_CHECKING:
    import pathlib

TAG: typ.Final[str] = "kani-snapshot-2026-11-aaaaaaaaaaaa-r1"
BUILDER: typ.Final[str] = "b" * 40
MANIFEST_SHA: typ.Final[str] = "c" * 64
RUN_ID: typ.Final[int] = 7
RUN_ATTEMPT: typ.Final[int] = 1
#: The build-input fingerprint the fixture release records in its body.
FINGERPRINT: typ.Final[str] = "d" * 64
#: A fingerprint for a different input set.
OTHER_FINGERPRINT: typ.Final[str] = "e" * 64

#: The exact payload bytes a successful publication must serve.
PAYLOADS: typ.Final[dict[str, bytes]] = {
    "backend.tar.gz": b"verified backend",
    "launcher.tar.gz": b"verified launcher",
    "notices.tar.gz": b"verified notices",
    "inventory.json": b'{"entries": []}',
}

#: The `gh api --jq` query the adapter's observation uses.
OBSERVE_QUERY: typ.Final[str] = (
    "{id, tag_name, draft, target_commitish, body, assets: "
    "[.assets[] | {id, name, size, digest}]}"
)

#: Failures the state machine may inject around any single effect.
FAILURE_OPERATIONS: typ.Final[tuple[str, ...]] = (
    "observe",
    "create_draft",
    "upload",
    "verify",
    "publish",
)

#: Every operation paired with each side of the effect it performs.
FAILURE_POINTS: typ.Final[tuple[str, ...]] = tuple(
    f"{operation}:{when}"
    for operation in FAILURE_OPERATIONS
    for when in ("before", "after")
)


def digest(data: bytes) -> str:
    """Return the lowercase SHA-256 of exact bytes."""
    return hashlib.sha256(data).hexdigest()


def body(
    *,
    tag: str = TAG,
    fingerprint: str = FINGERPRINT,
    run_id: int = RUN_ID,
    run_attempt: int = RUN_ATTEMPT,
) -> str:
    """Return the release body recording one origin and input set."""
    return release_body(
        tag,
        fingerprint,
        run_id=run_id,
        run_attempt=run_attempt,
    )


def expected(*, fingerprint: str = FINGERPRINT) -> ExpectedRelease:
    """Return the immutable identity the fixtures publish."""
    return ExpectedRelease(
        tag=TAG,
        builder_commit=BUILDER,
        assets=tuple(
            ReleaseAsset(name=name, size=len(data), sha256=digest(data))
            for name, data in PAYLOADS.items()
        ),
        manifest_sha256=MANIFEST_SHA,
        input_fingerprint=fingerprint,
        run_id=RUN_ID,
        run_attempt=RUN_ATTEMPT,
    )


def validation() -> ValidationBinding:
    """Return same-run validation evidence for the fixture release."""
    return ValidationBinding(
        run_id=RUN_ID,
        run_attempt=RUN_ATTEMPT,
        manifest_sha256=MANIFEST_SHA,
        assets=tuple(PAYLOADS),
        verification_passed=True,
    )


def observed_asset(name: str, *, sha256: str | None) -> ObservedAsset:
    """Return one observed asset carrying the fixture bytes' size."""
    return ObservedAsset(name=name, size=len(PAYLOADS[name]), sha256=sha256)


def placeholder(name: str, *, asset_id: int = 7) -> ObservedAsset:
    """Return the record an upload interrupted after creating it leaves behind."""
    return ObservedAsset(name=name, size=0, sha256=None, asset_id=asset_id)


def draft(
    assets: tuple[ObservedAsset, ...],
    *,
    body_text: str | None = body(),
    provenance_verified: bool = True,
    provenance_reason: str | None = None,
) -> ObservedRelease:
    """Return a draft observation carrying `assets` and its origin.

    `provenance_verified` defaults to `True` because these witnesses describe a
    completed verification; a witness for the unauthenticated case asks for
    `False` and gives the `provenance_reason` a failed verification reports.
    """
    return ObservedRelease(
        exists=True,
        is_draft=True,
        tag=TAG,
        target_commitish=BUILDER,
        assets=assets,
        release_id=99,
        body=body_text,
        provenance_verified=provenance_verified,
        provenance_reason=provenance_reason,
    )


def published(
    assets: tuple[ObservedAsset, ...],
    *,
    body_text: str | None = body(),
    provenance_verified: bool = True,
    provenance_reason: str | None = None,
) -> ObservedRelease:
    """Return a published release observed with verified provenance."""
    return ObservedRelease(
        exists=True,
        is_draft=False,
        tag=TAG,
        target_commitish=BUILDER,
        tag_commit=BUILDER,
        assets=assets,
        release_id=99,
        body=body_text,
        provenance_verified=provenance_verified,
        provenance_reason=provenance_reason,
    )


def verified_assets() -> tuple[ObservedAsset, ...]:
    """Return every expected asset with its correct digest."""
    return tuple(
        observed_asset(name, sha256=digest(data)) for name, data in PAYLOADS.items()
    )


def assets_with_digest(name: str, sha256: str) -> tuple[ObservedAsset, ...]:
    """Return the complete asset set with one asset carrying `sha256`."""
    return tuple(
        ObservedAsset(
            name=seen,
            size=len(data),
            sha256=sha256 if seen == name else digest(data),
        )
        for seen, data in PAYLOADS.items()
    )


def assets_with_size(name: str, size: int) -> tuple[ObservedAsset, ...]:
    """Return the complete asset set with one asset carrying `size`."""
    return tuple(
        ObservedAsset(
            name=seen,
            size=size if seen == name else len(data),
            sha256=digest(data),
        )
        for seen, data in PAYLOADS.items()
    )


def asset(name: str) -> ReleaseAsset:
    """Return the expected release asset with the fixture digest."""
    return ReleaseAsset(
        name=name, size=len(PAYLOADS[name]), sha256=digest(PAYLOADS[name])
    )


def write_payloads(directory: pathlib.Path) -> dict[str, pathlib.Path]:
    """Write the fixture payloads and return their paths."""
    directory.mkdir(parents=True, exist_ok=True)
    paths: dict[str, pathlib.Path] = {}
    for name, data in PAYLOADS.items():
        path = directory / name
        path.write_bytes(data)
        paths[name] = path
    return paths


def request(
    tmp_path: pathlib.Path,
    validation_binding: ValidationBinding | None = None,
) -> PublicationRequest:
    """Return a publication request whose local assets are exact."""
    return PublicationRequest(
        expected=expected(),
        validation=validation_binding or validation(),
        asset_paths=write_payloads(tmp_path / "assets"),
    )


def release_json(
    *,
    draft: bool,
    assets: list[dict[str, object]] | None = None,
    body_text: str | None = body(),
) -> str:
    """Return one release API document as `gh --jq` would print it."""
    return json.dumps({
        "id": 4242,
        "tag_name": TAG,
        "draft": draft,
        "target_commitish": BUILDER,
        "body": body_text,
        "assets": assets or [],
    })
