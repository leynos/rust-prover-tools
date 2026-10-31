"""Pinned prebuilt assets for Kani and Z3.

The Kani frontend comes from cargo-quickinstall, and the native bundle comes
from the Kani release. Z3 comes from the official Z3 release. Each asset
records its SHA-256 digest and a compressed-size ceiling so
`rust_prover_tools.downloads` can reject an oversized or altered response.
"""

from __future__ import annotations

import typing as typ

from rust_prover_tools.downloads import PinnedAsset

__all__ = (
    "COPY_CHUNK_SIZE",
    "KANI_BUNDLE_ASSET",
    "KANI_FRONTEND_ASSET",
    "KANI_FRONTEND_MEMBERS",
    "KANI_FRONTEND_MEMBER_MAX_BYTES",
    "KANI_TARGET",
    "PREBUILT_KANI_VERSION",
    "Z3_ASSET",
    "Z3_EXTRACTED_MAX_BYTES",
    "Z3_MEMBER",
    "Z3_TARGET",
    "Z3_VERSION",
)

COPY_CHUNK_SIZE: typ.Final[int] = 1024 * 1024

PREBUILT_KANI_VERSION: typ.Final[str] = "0.67.0"
KANI_TARGET: typ.Final[str] = "x86_64-unknown-linux-gnu"
KANI_FRONTEND_MEMBERS: typ.Final[tuple[str, ...]] = ("cargo-kani", "kani")
KANI_FRONTEND_MEMBER_MAX_BYTES: typ.Final[int] = 2 * 1024 * 1024

_KANI_FRONTEND_NAME: typ.Final[str] = (
    f"kani-verifier-{PREBUILT_KANI_VERSION}-{KANI_TARGET}.tar.gz"
)
_KANI_BUNDLE_NAME: typ.Final[str] = f"kani-{PREBUILT_KANI_VERSION}-{KANI_TARGET}.tar.gz"

KANI_FRONTEND_ASSET: typ.Final[PinnedAsset] = PinnedAsset(
    name=_KANI_FRONTEND_NAME,
    url=(
        "https://github.com/cargo-bins/cargo-quickinstall/releases/download/"
        f"kani-verifier-{PREBUILT_KANI_VERSION}/{_KANI_FRONTEND_NAME}"
    ),
    sha256="ed2bafc239b834e14c6b66fc4838e342e3bc0b814e548e72ea30e84f83dc0974",
    # The published archive is 508 KiB; leave headroom for a repacked release
    # without allowing an unbounded response if the digest pin is bypassed.
    max_bytes=4 * 1024 * 1024,
)

KANI_BUNDLE_ASSET: typ.Final[PinnedAsset] = PinnedAsset(
    name=_KANI_BUNDLE_NAME,
    url=(
        "https://github.com/model-checking/kani/releases/download/"
        f"kani-{PREBUILT_KANI_VERSION}/{_KANI_BUNDLE_NAME}"
    ),
    sha256="3b5f7afd3b51603ee720db7bc1bc4fe46b5a4f5d36daad9939c4b4c658b51ac0",
    # The published bundle is about 137 MiB.
    max_bytes=192 * 1024 * 1024,
)

Z3_VERSION: typ.Final[str] = "4.16.0"
Z3_TARGET: typ.Final[str] = "x64-glibc-2.39"
_Z3_ASSET_NAME: typ.Final[str] = f"z3-{Z3_VERSION}-{Z3_TARGET}.zip"

Z3_ASSET: typ.Final[PinnedAsset] = PinnedAsset(
    name=_Z3_ASSET_NAME,
    url=(
        "https://github.com/Z3Prover/z3/releases/download/"
        f"z3-{Z3_VERSION}/{_Z3_ASSET_NAME}"
    ),
    sha256="7288c49a5bd6dbafd7b0b0d1f65956b91672da24b08f09242919af159be3418e",
    # The published zip is about 50 MiB.
    max_bytes=96 * 1024 * 1024,
)

Z3_MEMBER: typ.Final[str] = f"z3-{Z3_VERSION}-{Z3_TARGET}/bin/z3"
Z3_EXTRACTED_MAX_BYTES: typ.Final[int] = 512 * 1024 * 1024
