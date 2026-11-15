"""Frozen models for the pinned upstream Kani snapshot build recipe.

The recipe is the single description of how a snapshot is produced: which
upstream release, which toolchain, which builder image, which prerequisites,
which runtime baseline and which licence evidence. Later milestones consume it
instead of restating versions, and its canonical digest is recorded in the
manifest so a changed input can never keep an unchanged identity.

This module holds only the records and their vocabulary; `recipe_rules` owns
the invariants. Keeping the two apart lets the records stay a faithful,
readable description of the recipe rather than a place where every field
argument is interleaved with policy.
"""

from __future__ import annotations

import dataclasses as dc
import typing as typ

if typ.TYPE_CHECKING:
    from rust_prover_tools.kani_snapshot.models import (
        PrerequisiteRecord,
        RuntimeIdentity,
        SubmoduleRecord,
    )

__all__ = (
    "ATTESTATION_VERIFIER",
    "COMPONENT_LICENCE",
    "LAUNCHER_BINARIES",
    "NOTICE_INVENTORY",
    "RECIPE_SCHEMA_VERSION",
    "REQUIRED_LICENCE_COMPONENTS",
    "REQUIRED_PREREQUISITES",
    "AttestationRecipe",
    "BuilderRecord",
    "LauncherRecipe",
    "LicenceRecord",
    "Recipe",
    "UpstreamIdentity",
)

#: The only recipe schema version this implementation understands.
RECIPE_SCHEMA_VERSION: typ.Final[int] = 1

#: Entry points a launcher archive must provide, in upstream's order.
LAUNCHER_BINARIES: typ.Final[tuple[str, str]] = ("kani", "cargo-kani")

#: Prerequisites without which the bundle cannot be built.
REQUIRED_PREREQUISITES: typ.Final[tuple[str, ...]] = ("cbmc", "kissat")

#: Components the published notices must cover.
REQUIRED_LICENCE_COMPONENTS: typ.Final[tuple[str, ...]] = (
    "kani",
    "rustc",
    "cbmc",
    "kissat",
)

#: Evidence source meaning "upstream's own licence notes list this component".
NOTICE_INVENTORY: typ.Final[str] = "licence-notes"

#: Evidence source meaning "the component ships its own licence file".
COMPONENT_LICENCE: typ.Final[str] = "component-licence"

#: The only attestation verifier the producer runs.
ATTESTATION_VERIFIER: typ.Final[str] = "gh"


@dc.dataclass(frozen=True, slots=True)
class UpstreamIdentity:
    """The upstream release a snapshot is built from.

    Attributes
    ----------
    repository : str
        Upstream repository, `model-checking/kani`.
    commit : str
        Full 40-hex commit the release tag points at.
    crate_version : str
        Cargo package version at that commit.
    tag : str
        Release tag, which must be `kani-<crate_version>`.
    published_at : str
        Release publication time in ISO-8601 UTC.
    toolchain_channel : str
        Dated nightly channel named by the commit's `rust-toolchain.toml`.
    rustc_version : str
        `rustc --version` line the pinned channel reports.
    """

    repository: str
    commit: str
    crate_version: str
    tag: str
    published_at: str
    toolchain_channel: str
    rustc_version: str


@dc.dataclass(frozen=True, slots=True)
class BuilderRecord:
    """The distribution image the bundle must be built on.

    Rust binaries link the builder's C library, so the image fixes the oldest
    glibc the published payload can run on. Recording it separates the
    measured floor of a local prototype from the floor a published bundle is
    allowed to claim.

    Attributes
    ----------
    image : str
        Distribution image label, `ubuntu-22.04`.
    glibc : str
        The image's glibc release as a major/minor pair.
    """

    image: str
    glibc: str


@dc.dataclass(frozen=True, slots=True)
class LauncherRecipe:
    """How the matching `kani` and `cargo-kani` launchers are produced.

    Attributes
    ----------
    package : str
        Cargo package that provides the launchers, `kani-verifier`.
    binaries : tuple[str, ...]
        Installed binary names, in archive order.
    archive_template : str
        Asset name template with `{version}` and `{target}` placeholders.
    """

    package: str
    binaries: tuple[str, ...]
    archive_template: str


@dc.dataclass(frozen=True, slots=True)
class AttestationRecipe:
    """The verifier command the attestation milestone must run verbatim.

    Attributes
    ----------
    verifier : str
        Verifier executable, exactly `gh`.
    command_prefix : tuple[str, ...]
        Leading argument vector every attestation check starts with.
    """

    verifier: str
    command_prefix: tuple[str, ...]


@dc.dataclass(frozen=True, slots=True)
class LicenceRecord:
    """One redistributed component and where its licence text is evidenced.

    Attributes
    ----------
    component : str
        Component name as recorded in the notices.
    licence : str
        SPDX expression or short licence name.
    source : str
        URL or path of the authoritative licence text.
    evidence : str
        One of `NOTICE_INVENTORY` or `COMPONENT_LICENCE`, naming which kind
        of evidence backs this record.
    """

    component: str
    licence: str
    source: str
    evidence: str


@dc.dataclass(frozen=True, slots=True)
class Recipe:
    """A fully pinned snapshot build recipe.

    Attributes
    ----------
    schema_version : int
        Recipe schema version. Only version 1 is accepted.
    upstream : UpstreamIdentity
        Upstream release and toolchain identity.
    target : str
        The single supported target triple.
    compiler_floor : tuple[int, int]
        Oldest Rust release the snapshot promises to compile.
    builder : BuilderRecord
        Distribution image the published bundle must be built on.
    prerequisites : tuple[PrerequisiteRecord, ...]
        Pinned build prerequisites and their artifact digests.
    submodules : tuple[SubmoduleRecord, ...]
        Recursive upstream submodule gitlinks, sorted by path.
    runtime : RuntimeIdentity
        Measured runtime compatibility of the built payloads.
    launcher : LauncherRecipe
        How the matching launchers are produced.
    attestation : AttestationRecipe
        The verifier command the attestation milestone must run.
    licences : tuple[LicenceRecord, ...]
        Redistributed components and their licence evidence.
    """

    schema_version: int
    upstream: UpstreamIdentity
    target: str
    compiler_floor: tuple[int, int]
    builder: BuilderRecord
    prerequisites: tuple[PrerequisiteRecord, ...]
    submodules: tuple[SubmoduleRecord, ...]
    runtime: RuntimeIdentity
    launcher: LauncherRecipe
    attestation: AttestationRecipe
    licences: tuple[LicenceRecord, ...]
