"""Frozen records for the local setup receipt.

The receipt is evidence about this machine, not a trust anchor. Fields describe
what the reviewed pin and manifest require and what setup actually produced.
"""

from __future__ import annotations

import dataclasses as dc
import typing as typ

if typ.TYPE_CHECKING:
    from pathlib import Path

__all__ = (
    "ObservedToolchain",
    "ReceiptAsset",
    "SetupEvidence",
    "SetupReceipt",
)


@dc.dataclass(frozen=True, slots=True)
class ReceiptAsset:
    """One payload recorded by name, size and digest.

    Attributes
    ----------
    name : str
        Release asset basename.
    size : int
        Exact byte length.
    sha256 : str
        Lowercase SHA-256 of the exact bytes.
    """

    name: str
    size: int
    sha256: str


@dc.dataclass(frozen=True, slots=True)
class ObservedToolchain:
    """Compiler identities observed after setup completed.

    Attributes
    ----------
    rustc_vv : str
        Exact `rustc -vV` output observed from the installed compiler.
    kani_compiler : str
        Exact identity reported by the installed `kani-compiler`.
    """

    rustc_vv: str
    kani_compiler: str


@dc.dataclass(frozen=True, slots=True)
class SetupEvidence:
    """Observed results that only a completed setup can produce.

    Attributes
    ----------
    observed : ObservedToolchain
        Compiler identities observed from the installed generation.
    toolchain_link_target : Path
        Absolute target of setup's `kani-<VERSION>/toolchain` symlink.
    """

    observed: ObservedToolchain
    toolchain_link_target: Path


@dc.dataclass(frozen=True, slots=True)
class SetupReceipt:
    """The validated local receipt for one generation.

    Attributes
    ----------
    schema_version : int
        Receipt schema version. Only version 1 is accepted.
    tag : str
        Immutable snapshot tag.
    upstream_commit : str
        Upstream Kani commit.
    builder_commit : str
        Builder commit from the pin.
    manifest_sha256 : str
        Digest of the reviewed manifest bytes.
    target : str
        Rust target triple.
    backend_version : str
        Upstream Kani package version.
    toolchain : str
        Rust toolchain identifier installed by setup.
    rustc_vv : str
        Expected `rustc -vV` identity from the manifest.
    kani_compiler : str
        Expected `kani-compiler` identity from the manifest.
    generation : Path
        Absolute generation directory.
    kani_home : Path
        Generation-private `KANI_HOME`.
    rustup_home : Path
        Generation-private `RUSTUP_HOME`.
    cargo_home : Path
        Generation-private `CARGO_HOME`.
    launcher : Path
        Absolute `kani` launcher.
    driver : Path
        Absolute `kani-driver`.
    compiler : Path
        Absolute `kani-compiler`.
    rustc : Path
        Absolute `rustc` from the pinned toolchain.
    toolchain_link_target : Path
        Absolute target of setup's `kani-<VERSION>/toolchain` symlink.
    observed : ObservedToolchain
        Identities observed on this machine.
    assets : tuple[ReceiptAsset, ...]
        Complete manifest payload set in manifest order.
    """

    schema_version: int
    tag: str
    upstream_commit: str
    builder_commit: str
    manifest_sha256: str
    target: str
    backend_version: str
    toolchain: str
    rustc_vv: str
    kani_compiler: str
    generation: Path
    kani_home: Path
    rustup_home: Path
    cargo_home: Path
    launcher: Path
    driver: Path
    compiler: Path
    rustc: Path
    toolchain_link_target: Path
    observed: ObservedToolchain
    assets: tuple[ReceiptAsset, ...]
