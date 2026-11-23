"""Shared fixtures for the snapshot download tests.

The release policy and its pinned asset describe one release identity, so the
policy and retry suites both build them here rather than each keeping a copy.
"""

from __future__ import annotations

from rust_prover_tools.downloads import PinnedAsset
from rust_prover_tools.kani_snapshot.download import ReleaseUrlPolicy
from tests.kani_snapshot_fixtures import TAG

__all__ = ("ASSET_NAME", "RELEASE_URL", "REPOSITORY", "TAG", "asset", "policy")

ASSET_NAME = "backend.tar.gz"
REPOSITORY = "leynos/rust-prover-tools"
RELEASE_URL = f"https://github.com/{REPOSITORY}/releases/download/{TAG}/{ASSET_NAME}"


def policy() -> ReleaseUrlPolicy:
    """Return the release policy under test."""
    return ReleaseUrlPolicy(repository=REPOSITORY, tag=TAG, asset_name=ASSET_NAME)


def asset() -> PinnedAsset:
    """Return a pinned asset whose transport is replaced in retry tests."""
    return PinnedAsset(
        name=ASSET_NAME,
        url=RELEASE_URL,
        sha256="0" * 64,
        max_bytes=1024,
    )
