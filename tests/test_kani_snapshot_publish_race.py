"""Publication is decided from state confirmed after the bytes were read.

The recheck observes a draft and then downloads what it reported, so the draft
can change after its assets were read but before the write. The verified set
then describes a release that no longer exists, and publishing from it would
make an incomplete or retargeted release public before anyone noticed. These
tests hold the boundary that the state which authorises the write is read again
once the download completes: an asset that disappeared during the read is
restored, and a draft whose target or recorded attempt moved is refused, before
anything becomes public.

The stale-reading case, where the removal happens before the recheck observes,
lives in `test_kani_snapshot_publish`.
"""

from __future__ import annotations

import typing as typ

import pytest

from rust_prover_tools.errors import ProverToolError
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


class _DraftMutatedDuringRecheckApi(FakeReleaseApi):
    """Apply one metadata change after the recheck has hashed the bytes.

    The asset set is left untouched, so only a reading that compares the
    release's identity and body can tell the two observations apart.
    """

    def __init__(self, mutate: cabc.Callable[[FakeReleaseApi], None]) -> None:
        """Create the fake with the change still pending."""
        super().__init__()
        self._mutate = mutate
        self._verifications = 0

    def verify(
        self,
        tag: str,
        observed: ObservedRelease,
        names: cabc.Sequence[str],
    ) -> VerifiedAssets:
        """Verify the reported assets, then change the draft's metadata."""
        verified = super().verify(tag, observed, names)
        self._verifications += 1
        if self._verifications == 2:
            self._mutate(self)
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


def _retarget(api: FakeReleaseApi) -> None:
    """Point the draft at a commit this run never built."""
    api.target_commitish = "c" * 40


def _rebind(api: FakeReleaseApi) -> None:
    """Rewrite the draft's body so it records another attempt."""
    api.body_text = fixtures.body(run_attempt=2)


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (_retarget, "another builder commit"),
        (_rebind, "another workflow run or attempt"),
    ],
    ids=["retargeted", "rebound"],
)
def test_a_draft_changed_during_the_recheck_is_refused_before_publishing(
    tmp_path: pathlib.Path,
    mutate: cabc.Callable[[FakeReleaseApi], None],
    reason: str,
) -> None:
    """Metadata the policy reads is compared across the download as well.

    The asset identities are unchanged in both cases, so a comparison limited
    to the asset set would publish a draft whose target or recorded attempt no
    longer matches the run that validated it. The policy refuses the newer
    reading instead, and nothing becomes public.
    """
    request = _request(tmp_path)
    api = _DraftMutatedDuringRecheckApi(mutate)
    api.place_draft()
    for name, data in fixtures.PAYLOADS.items():
        api.stored[name] = data

    with pytest.raises(ProverToolError, match="publication conflict") as raised:
        publish_release(request, api)

    assert reason in str(raised.value), "the refusal did not name the changed field"
    assert api.calls.count("publish") == 0, "a changed draft was published"
