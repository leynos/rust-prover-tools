"""The local bytes one publication attempt is asked to publish.

Publication authenticates the release it finds; it must also refuse to upload
bytes the manifest does not describe. Both facts are decided from the request
alone, before any release API is called, so a tampered or missing local asset
fails without creating, editing or publishing anything.

This module holds the request record and that precondition. The orchestration
that consumes it lives in `publish`.
"""

from __future__ import annotations

import dataclasses as dc
import typing as typ

from rust_prover_tools.downloads import sha256_file
from rust_prover_tools.errors import ProverToolError

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

    from rust_prover_tools.kani_snapshot.release_policy import (
        ExpectedRelease,
        ReleaseAsset,
        ValidationBinding,
    )

__all__ = ("PublicationRequest", "require_local_assets")


@dc.dataclass(frozen=True, slots=True)
class PublicationRequest:
    """Everything one publication attempt needs.

    Attributes
    ----------
    expected : ExpectedRelease
        Immutable identity to publish.
    validation : ValidationBinding
        Same-run clean-validation evidence.
    asset_paths : collections.abc.Mapping of str to Path
        Local file for each expected asset name.
    """

    expected: ExpectedRelease
    validation: ValidationBinding
    asset_paths: cabc.Mapping[str, Path]


def _require_local_asset(asset: ReleaseAsset, path: Path | None) -> None:
    """Require one local file to hold exactly the bytes the manifest records."""
    if path is None:
        msg = f"publication is missing local asset {asset.name!r}"
        raise ProverToolError(msg)
    try:
        size = path.stat().st_size
    except OSError as exc:
        msg = f"failed to stat release asset '{path}': {exc}"
        raise ProverToolError(msg) from exc
    if size != asset.size or sha256_file(path) != asset.sha256:
        msg = f"local release asset {asset.name!r} does not match its manifest"
        raise ProverToolError(msg)


def require_local_assets(request: PublicationRequest) -> None:
    """Require every local upload to match the expected bytes before any call."""
    names = {asset.name for asset in request.expected.assets}
    extra = sorted(set(request.asset_paths) - names)
    if extra:
        msg = f"publication names local assets outside the expected set: {extra}"
        raise ProverToolError(msg)
    for asset in request.expected.assets:
        _require_local_asset(asset, request.asset_paths.get(asset.name))
