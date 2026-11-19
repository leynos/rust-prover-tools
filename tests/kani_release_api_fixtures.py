"""A deterministic release API double with before/after failure injection.

The fake's stored bytes are the server's truth. `observe` is a metadata query:
it reports asset sizes and, when asked, the digests the API volunteers, but it
never claims the release's evidence authenticated — only `verify` can do that.
`publish` refuses unless every expected asset is stored with the exact bytes,
which makes publish-before-upload an error rather than a silent success.

The double lives apart from `tests.kani_publish_fixtures` so the shared values
that describe one release stay separate from the mutable server state and
failure schedule the state machine drives.
"""

from __future__ import annotations

import typing as typ

from rust_prover_tools.kani_snapshot.release_model import VerifiedAssets
from rust_prover_tools.kani_snapshot.release_policy import (
    ExpectedRelease,
    ObservedAsset,
    ObservedRelease,
    ReleaseAsset,
    ReleaseTransientError,
)
from tests.kani_publish_fixtures import BUILDER, FINGERPRINT, PAYLOADS, body, digest

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import pathlib


class FakeReleaseApiError(AssertionError):
    """The orchestration asked the fake to perform an impossible effect."""


class FakeReleaseApi:
    """A deterministic release API with before/after failure injection.

    The stored bytes are the server's truth. `observe` is a metadata query: it
    reports asset sizes and, when `digests_visible` is set, the digests the API
    volunteers, but it never claims the release's evidence authenticated —
    only `verify` can do that. `publish` refuses unless every expected asset is
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
        self.placeholders: dict[str, int] = {}
        self.calls: list[str] = []
        self.uploads: list[str] = []
        self.discards: list[str] = []
        self.hashes: list[str] = []
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

    def _evidence_verified(self) -> bool:
        """Whether the served bytes would authenticate the release's evidence.

        The fake stores payloads only, so serving exactly the expected bytes
        stands in for a release whose manifest, checksums and attestation
        verify. A draft is authenticated the same way a published release is:
        the adapter verifies whatever bytes the release serves, so publication
        never has to trust an unverified draft.
        """
        return self.provenance_verified or (
            set(self.stored) == set(PAYLOADS)
            and all(self.stored[name] == data for name, data in PAYLOADS.items())
        )

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
        ) + tuple(
            ObservedAsset(name=name, size=0, sha256=None, asset_id=asset_id)
            for name, asset_id in sorted(self.placeholders.items())
        )
        result = ObservedRelease(
            exists=True,
            is_draft=self.is_draft,
            tag=tag,
            target_commitish=BUILDER,
            assets=assets,
            release_id=99,
            body=self.body_text,
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

    def place_placeholder(self, name: str, *, asset_id: int = 7) -> None:
        """Leave the record an upload interrupted after creating it produces."""
        self.placeholders[name] = asset_id

    def discard(self, tag: str, asset: ObservedAsset) -> None:
        """Delete one placeholder record, as the API requires before re-upload."""
        self._enter("discard")
        if self.placeholders.pop(asset.name, None) is None:
            msg = f"discard named {asset.name!r}, which is not a placeholder"
            raise FakeReleaseApiError(msg)
        self.discards.append(asset.name)
        self._leave("discard")

    def upload(self, tag: str, asset: ReleaseAsset, path: pathlib.Path) -> None:
        """Store the uploaded bytes exactly as the release would serve them."""
        self._enter("upload")
        if asset.name in self.placeholders:
            msg = f"upload of {asset.name!r} preceded its placeholder delete"
            raise FakeReleaseApiError(msg)
        self.stored[asset.name] = path.read_bytes()
        self.uploads.append(asset.name)
        self._leave("upload")

    def hash_assets(
        self,
        tag: str,
        observed: ObservedRelease,
        names: cabc.Sequence[str],
    ) -> tuple[tuple[str, str], ...]:
        """Download only the named assets and report their computed digests.

        This is the byte check a draft edit is gated on, not a provenance
        verdict: nothing here claims the release's evidence authenticated the
        manifest.
        """
        self._enter("hash_assets")
        missing = [name for name in names if name not in self.stored]
        if missing:
            msg = f"release asset {missing[0]!r} is no longer downloadable"
            raise ReleaseTransientError(msg)
        digests = tuple((name, digest(self.stored[name])) for name in names)
        self.hashes.extend(names)
        self._leave("hash_assets")
        return digests

    def verify(
        self,
        tag: str,
        observed: ObservedRelease,
        names: cabc.Sequence[str],
    ) -> VerifiedAssets:
        """Download the named assets and report their digests and provenance.

        An asset listed but no longer stored cannot be downloaded, which is a
        transient transport failure rather than a verdict about the release:
        the orchestration re-observes and reacts to the state it finds.
        """
        self._enter("verify")
        missing = [name for name in names if name not in self.stored]
        if missing:
            msg = f"release asset {missing[0]!r} is no longer downloadable"
            raise ReleaseTransientError(msg)
        digests = tuple((name, digest(self.stored[name])) for name in names)
        verified = self._evidence_verified()
        self._leave("verify")
        return VerifiedAssets(
            digests=digests,
            provenance_verified=verified,
            reason=None if verified else "served bytes do not match the manifest",
        )

    def _require_publishable(self) -> None:
        """Refuse to publish anything that is not a complete, exact draft.

        The asset set is checked before any individual byte is read: a missing
        asset must be reported as an early publish call, not as a `KeyError`
        from comparing bytes that were never stored.
        """
        if not self.exists:
            msg = "publish was called without a release"
            raise FakeReleaseApiError(msg)
        if not self.is_draft:
            msg = "publish was called without a draft"
            raise FakeReleaseApiError(msg)
        if self.placeholders or set(self.stored) != set(PAYLOADS):
            msg = "publish preceded an upload"
            raise FakeReleaseApiError(msg)
        if any(self.stored[name] != data for name, data in PAYLOADS.items()):
            msg = "publish was called with foreign bytes"
            raise FakeReleaseApiError(msg)

    def publish(self, tag: str, release_id: int) -> None:
        """Publish a complete, exact draft and report it as authenticated."""
        self._enter("publish")
        self._require_publishable()
        self.is_draft = False
        # Publishing is the last moment the adapter can authenticate the
        # release's own bytes; the observation that follows reports it.
        self.provenance_verified = True
        self._leave("publish")
