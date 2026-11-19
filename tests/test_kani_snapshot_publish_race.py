"""Publication is decided from state confirmed after the bytes were read.

The recheck observes a draft and then downloads what it reported, so an asset
can be removed after its own download but before the write. The verified set
then describes a release that no longer exists, and publishing from it would
make an incomplete release public before anyone noticed. These tests hold the
boundary that the state which authorises the write is read again once the
download completes; an asset that disappeared during the read is restored
before anything becomes public.

The stale-reading case, where the removal happens before the recheck observes,
lives in `test_kani_snapshot_publish`.
"""

from __future__ import annotations

import typing as typ

from rust_prover_tools.kani_snapshot.publish import (
    PublicationRequest,
    publish_release,
)
from rust_prover_tools.kani_snapshot.release_policy import (
    ObservedRelease,
    ReleaseAction,
    VerifiedAssets,
)
from tests import kani_publish_fixtures as fixtures
from tests.kani_release_api_fixtures import FakeReleaseApi

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import pathlib


class _AssetRemovedDuringRecheckApi(FakeReleaseApi):
    """Remove one asset after the download that hashed it.

    The removal happens on the second verification: the first completes the
    draft, the second is the recheck immediately before the publish call.
    Deleting the stored bytes after the digest has been computed is the case a
    reading taken before the download cannot see.
    """

    def __init__(self) -> None:
        """Create the fake with the removal still pending."""
        super().__init__()
        self._verifications = 0

    def verify(
        self,
        tag: str,
        observed: ObservedRelease,
        names: cabc.Sequence[str],
    ) -> VerifiedAssets:
        """Verify the reported assets, then remove one of them."""
        verified = super().verify(tag, observed, names)
        self._verifications += 1
        if self._verifications == 2:
            self.stored.pop("backend.tar.gz", None)
        return verified


def _request(tmp_path: pathlib.Path) -> PublicationRequest:
    """Return a publication request whose local assets are exact."""
    return fixtures.request(tmp_path)


def test_an_asset_removed_during_the_recheck_is_restored_before_publishing(
    tmp_path: pathlib.Path,
) -> None:
    """The write is decided from state read after the download, not before it.

    The recheck's observation still reported every asset, so its verified set
    looks complete. Only the asset that vanished after its download can tell
    the two readings apart, and publishing from the stale one would leave the
    release permanently incomplete.
    """
    request = _request(tmp_path)
    api = _AssetRemovedDuringRecheckApi()
    api.place_draft()
    for name, data in fixtures.PAYLOADS.items():
        api.stored[name] = data

    decision = publish_release(request, api)

    assert decision.action is ReleaseAction.NOOP, "the draft did not converge"
    assert api.stored == fixtures.PAYLOADS, (
        "the asset removed during the recheck was not restored"
    )
    assert api.uploads.count("backend.tar.gz") == 1, (
        "the vanished asset was not uploaded again"
    )
    assert api.calls.count("publish") == 1, "publication was attempted twice"
