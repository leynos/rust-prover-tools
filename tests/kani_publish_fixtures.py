"""Shared fixtures for the immutable snapshot publication tests.

The publication tests all describe one exact release: four payloads with known
bytes, one builder commit and one validated run. Keeping those values here means
a policy witness, an orchestration test and a generated state machine cannot
drift into describing different releases, which would make a passing test prove
nothing about the others.

`FakeReleaseApi` is deliberately part of the fixtures. Its stored bytes are the
server's truth, so every test that drives the real orchestration shares one
definition of "what the release would serve".
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
    ReleaseTransientError,
    ValidationBinding,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc
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
    "[.assets[] | {name, size, digest}]}"
)

#: Failures the state machine may inject around any single effect.
FAILURE_OPERATIONS: typ.Final[tuple[str, ...]] = (
    "observe",
    "create_draft",
    "upload",
    "verify_assets",
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


def draft(
    assets: tuple[ObservedAsset, ...],
    *,
    body_text: str | None = body(),
) -> ObservedRelease:
    """Return a draft observation carrying `assets` and its origin."""
    return ObservedRelease(
        exists=True,
        is_draft=True,
        tag=TAG,
        target_commitish=BUILDER,
        assets=assets,
        release_id=99,
        body=body_text,
    )


def published(
    assets: tuple[ObservedAsset, ...],
    *,
    body_text: str | None = body(),
    provenance_verified: bool = True,
) -> ObservedRelease:
    """Return a published release observed with verified provenance."""
    return ObservedRelease(
        exists=True,
        is_draft=False,
        tag=TAG,
        target_commitish=BUILDER,
        assets=assets,
        release_id=99,
        body=body_text,
        provenance_verified=provenance_verified,
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


class FakeReleaseApiError(AssertionError):
    """The orchestration asked the fake to perform an impossible effect."""


class FakeReleaseApi:
    """A deterministic release API with before/after failure injection.

    The stored bytes are the server's truth. `observe` reports them with
    digests when `digests_visible` is set, so a test can require the policy to
    verify before publishing. `publish` refuses unless every expected asset is
    stored with the exact bytes, which makes publish-before-upload an error
    rather than a silent success.
    """

    def __init__(self, *, digests_visible: bool = True) -> None:
        """Create an absent release with no stored assets."""
        self.digests_visible = digests_visible
        self.exists = False
        self.is_draft = False
        self.body_text = body()
        self.provenance_verified = False
        self.stored: dict[str, bytes] = {}
        self.calls: list[str] = []
        self.uploads: list[str] = []
        self.failures: dict[str, int] = {}

    def fail_next(self, operation: str, *, after: bool = False) -> None:
        """Inject one transient failure around the next `operation` call."""
        key = f"{operation}:{'after' if after else 'before'}"
        self.failures[key] = self.failures.get(key, 0) + 1

    def _enter(self, operation: str) -> None:
        """Record a call and raise a transient failure when scheduled."""
        self.calls.append(operation)
        key = f"{operation}:before"
        if self.failures.get(key, 0):
            self.failures[key] -= 1
            raise ReleaseTransientError(operation)

    def _leave(self, operation: str) -> None:
        """Raise a transient failure after an effect that may have landed."""
        key = f"{operation}:after"
        if self.failures.get(key, 0):
            self.failures[key] -= 1
            raise ReleaseTransientError(operation)

    def observe(self, tag: str) -> ObservedRelease:
        """Return the stored state, with digests computed from stored bytes."""
        self._enter("observe")
        if not self.exists:
            self._leave("observe")
            return ObservedRelease(exists=False, is_draft=False)
        assets = tuple(
            ObservedAsset(
                name=name,
                size=len(data),
                sha256=digest(data) if self.digests_visible else None,
            )
            for name, data in sorted(self.stored.items())
        )
        result = ObservedRelease(
            exists=True,
            is_draft=self.is_draft,
            tag=tag,
            target_commitish=BUILDER,
            assets=assets,
            release_id=99,
            body=self.body_text,
            provenance_verified=not self.is_draft and self.provenance_verified,
        )
        self._leave("observe")
        return result

    def create_draft(self, expected: ExpectedRelease) -> None:
        """Create the draft state recording the inputs it was created from."""
        self._enter("create_draft")
        self.place_draft(fingerprint=expected.input_fingerprint)
        self._leave("create_draft")

    def place_draft(self, *, fingerprint: str = FINGERPRINT) -> None:
        """Create the draft state directly, as a previous attempt would have."""
        self.exists = True
        self.is_draft = True
        self.body_text = body(fingerprint=fingerprint)

    def place_published(self, *, fingerprint: str = FINGERPRINT) -> None:
        """Place the fixture release as already published and verified."""
        self.exists = True
        self.is_draft = False
        self.body_text = body(fingerprint=fingerprint)
        self.stored = dict(PAYLOADS)
        self.provenance_verified = True

    def place_asset(self, name: str, path: pathlib.Path) -> None:
        """Store one asset directly, as a previous attempt would have."""
        self.stored[name] = path.read_bytes()

    def upload(self, tag: str, asset: ReleaseAsset, path: pathlib.Path) -> None:
        """Store the uploaded bytes exactly as the release would serve them."""
        self._enter("upload")
        self.stored[asset.name] = path.read_bytes()
        self.uploads.append(asset.name)
        self._leave("upload")

    def verify_assets(self, tag: str, names: cabc.Sequence[str]) -> dict[str, str]:
        """Return the digest of each stored asset, as a download would."""
        self._enter("verify_assets")
        digests = {name: digest(self.stored[name]) for name in names}
        self._leave("verify_assets")
        return digests

    def _require_publishable(self) -> None:
        """Refuse to publish anything that is not a complete, exact draft."""
        checks = (
            (self.exists, "publish was called without a release"),
            (self.is_draft, "publish was called without a draft"),
            (set(self.stored) == set(PAYLOADS), "publish preceded an upload"),
            (
                all(self.stored[name] == data for name, data in PAYLOADS.items()),
                "publish was called with foreign bytes",
            ),
        )
        for satisfied, message in checks:
            if not satisfied:
                raise FakeReleaseApiError(message)

    def publish(self, tag: str, release_id: int) -> None:
        """Publish a complete, exact draft and report it as authenticated."""
        self._enter("publish")
        self._require_publishable()
        self.is_draft = False
        # Publishing is the last moment the adapter can authenticate the
        # release's own bytes; the observation that follows reports it.
        self.provenance_verified = True
        self._leave("publish")
