"""A draft is hashed before it is edited, and only then repaired.

An asset the API lists without a digest is a claim, not evidence: its size can
match the manifest while its bytes do not. These tests drive the real
orchestration over a release API that volunteers no digests, so the boundary
they hold is that a draft is repaired only after the bytes already present have
been downloaded and hashed. A draft that turns out to conflict is refused with
no discard, upload or publish, and a draft whose bytes match still recovers.

The complete-draft verification path lives in `test_kani_snapshot_publish`, and
the pure decision rules are witnessed in `test_kani_snapshot_release_policy`.
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
)
from tests import kani_publish_fixtures as fixtures
from tests.kani_release_api_fixtures import FakeReleaseApi

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import pathlib


def _request(tmp_path: pathlib.Path) -> PublicationRequest:
    """Return a publication request whose local assets are exact."""
    return fixtures.request(tmp_path)


def _asset_id(api: FakeReleaseApi, name: str) -> int | None:
    """Return the asset identity the release reports for `name`.

    Reading it through `observe` keeps the test on the same surface the
    orchestration compares, rather than on the fake's private records.
    """
    observed = api.observe(fixtures.TAG)
    return next(asset.asset_id for asset in observed.assets if asset.name == name)


class _AssetReplacedDuringHashApi(FakeReleaseApi):
    """Replace one asset's record while its bytes are being downloaded.

    A concurrent writer deletes and re-creates the asset, so the replacement
    carries a new id. The size is unchanged, which is what makes the stale
    digests look current: only the asset identity says the reading moved.
    """

    def __init__(self, *, name: str, data: bytes) -> None:
        """Create the fake, arming it with a replacement for `name`."""
        super().__init__(digests_visible=False)
        self.replaced_name = name
        self.replacement = data
        self.replaced = False

    def hash_assets(
        self,
        tag: str,
        observed: ObservedRelease,
        names: cabc.Sequence[str],
    ) -> tuple[tuple[str, str], ...]:
        """Hash the old bytes, then swap the asset before returning them."""
        digests = super().hash_assets(tag, observed, names)
        if not self.replaced:
            self.replaced = True
            self.replace_asset(self.replaced_name, self.replacement)
        return digests


def test_a_placeholder_is_not_discarded_before_unhashed_bytes_are_checked(
    tmp_path: pathlib.Path,
) -> None:
    """An unhashed wrong-byte asset is refused before the draft is edited.

    The API did not volunteer a digest for the stored assets, so their bytes
    are a claim. A same-size asset with different bytes must be found by
    downloading it before the recoverable placeholder is deleted, otherwise a
    conflicting draft is edited and the failure is reported after effects its
    state did not authorise.
    """
    request = _request(tmp_path)
    api = FakeReleaseApi(digests_visible=False)
    api.place_draft()
    for name in ("notices.tar.gz", "inventory.json"):
        api.place_asset(name, request.asset_paths[name])
    api.stored["backend.tar.gz"] = b"x" * len(fixtures.PAYLOADS["backend.tar.gz"])
    api.place_placeholder("launcher.tar.gz")

    with pytest.raises(ProverToolError, match="publication conflict"):
        publish_release(request, api)

    assert "hash_assets" in api.calls, "the unhashed asset was never downloaded"
    assert not api.discards, "a conflicting draft was edited"
    assert not api.uploads, "a conflicting draft was uploaded into"
    assert api.calls.count("publish") == 0


def test_an_unhashed_placeholder_draft_still_recovers(
    tmp_path: pathlib.Path,
) -> None:
    """Hashing before the edit must not turn a repairable draft into a conflict.

    When the stored bytes do match the manifest, discarding the placeholder
    and uploading the missing file still converge, even though the API
    reported no digests at all.
    """
    request = _request(tmp_path)
    api = FakeReleaseApi(digests_visible=False)
    api.place_draft()
    for name in ("launcher.tar.gz", "notices.tar.gz", "inventory.json"):
        api.place_asset(name, request.asset_paths[name])
    api.place_placeholder("backend.tar.gz")

    decision = publish_release(request, api)

    assert decision.action is ReleaseAction.NOOP, (
        "an explicitly hashed draft did not recover"
    )
    assert api.discards == ["backend.tar.gz"]
    assert api.uploads == ["backend.tar.gz"]
    assert api.stored == fixtures.PAYLOADS


def test_hashes_of_replaced_bytes_do_not_authorise_the_edit(
    tmp_path: pathlib.Path,
) -> None:
    """A hash taken across a replacement must not authorise changing the draft.

    The placeholder makes the draft repairable, so the orchestration first
    hashes the assets already present. While they download, a concurrent writer
    replaces `launcher.tar.gz` with different bytes of the same length. The
    digests just computed say the draft is sound, but they describe bytes that
    are no longer there; acting on them would delete the placeholder and upload
    into a draft nobody has checked. The confirming read must see the new asset
    record, discard the verdict, and refuse once the fresh bytes are hashed.
    """
    request = _request(tmp_path)
    launched = fixtures.PAYLOADS["launcher.tar.gz"]
    api = _AssetReplacedDuringHashApi(
        name="launcher.tar.gz",
        data=b"y" * len(launched),
    )
    api.place_draft()
    for name in ("launcher.tar.gz", "notices.tar.gz", "inventory.json"):
        api.place_asset(name, request.asset_paths[name])
    api.place_placeholder("backend.tar.gz")
    before = _asset_id(api, "launcher.tar.gz")
    assert before is not None, (
        "a placed asset must carry the identity GitHub gives it, otherwise the"
        " conflict below could come from an id appearing rather than changing"
    )

    with pytest.raises(ProverToolError, match="publication conflict"):
        publish_release(request, api)

    assert api.replaced, "the replacement never happened, so nothing was tested"
    assert _asset_id(api, "launcher.tar.gz") != before, (
        "the replacement must be visible as a new asset identity"
    )
    assert not api.discards, "a draft whose bytes moved was edited"
    assert not api.uploads, "a draft whose bytes moved was uploaded into"
    assert api.calls.count("hash_assets") >= 2, (
        "the replaced asset was never hashed again"
    )


def test_a_transient_hash_failure_is_retried(tmp_path: pathlib.Path) -> None:
    """Hashing is a read, so a transport failure is retried, not reported."""
    request = _request(tmp_path)
    api = FakeReleaseApi(digests_visible=False)
    api.place_draft()
    api.place_asset("backend.tar.gz", request.asset_paths["backend.tar.gz"])
    api.fail_next("hash_assets")
    decision = publish_release(request, api)
    assert decision.action is ReleaseAction.NOOP
    assert api.calls.count("hash_assets") >= 2
