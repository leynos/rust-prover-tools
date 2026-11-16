"""The pinned upstream Kani snapshot build recipe and its canonical form.

The values here are the EP-M1 feasibility result. They were established by
building the released `kani-0.68.0` tag locally and are evidenced by the
fixtures under `tests/fixtures/kani_recipe`: whoever changes a value must also
change the upstream file that justifies it, and the unit tests compare the two.

`pinned_recipe` validates before returning, so a bad edit fails at the first
call rather than after a long build. `recipe_to_json` is the byte-stable form
whose digest later milestones record in the producer manifest.
"""

from __future__ import annotations

import hashlib
import json
import typing as typ

from rust_prover_tools.kani_snapshot.models import (
    PrerequisiteRecord,
    RuntimeIdentity,
    SubmoduleRecord,
)
from rust_prover_tools.kani_snapshot.producer.recipe_evidence import (
    PINNED_SUBMODULES,
    PINNED_UPSTREAM_COMMIT,
)
from rust_prover_tools.kani_snapshot.producer.recipe_model import (
    COMPONENT_LICENCE,
    NOTICE_INVENTORY,
    RECIPE_SCHEMA_VERSION,
    AttestationRecipe,
    BuilderRecord,
    LauncherRecipe,
    LicenceRecord,
    Recipe,
    UpstreamIdentity,
)
from rust_prover_tools.kani_snapshot.producer.recipe_rules import validate_recipe

__all__ = (
    "PINNED_UPSTREAM_COMMIT",
    "PINNED_UPSTREAM_TAG",
    "pinned_recipe",
    "recipe_digest",
    "recipe_to_json",
)

#: The upstream release tag naming `PINNED_UPSTREAM_COMMIT`, which validation
#: reads from `recipe_evidence` so the rule and this recipe share one source.
PINNED_UPSTREAM_TAG: typ.Final[str] = "kani-0.68.0"

_PINNED: typ.Final[Recipe] = Recipe(
    schema_version=RECIPE_SCHEMA_VERSION,
    upstream=UpstreamIdentity(
        repository="model-checking/kani",
        commit=PINNED_UPSTREAM_COMMIT,
        crate_version="0.68.0",
        tag=PINNED_UPSTREAM_TAG,
        published_at="2026-09-16T03:43:14Z",
        toolchain_channel="nightly-2026-08-21",
        rustc_version="rustc 1.100.0-nightly (8925ea358 2026-08-20)",
    ),
    target="x86_64-unknown-linux-gnu",
    compiler_floor=(1, 94),
    builder=BuilderRecord(image="ubuntu-22.04", glibc="2.35"),
    prerequisites=(
        PrerequisiteRecord(
            name="cbmc",
            source=(
                "https://github.com/diffblue/cbmc/releases/download/cbmc-6.11.0/"
                "ubuntu-22.04-cbmc-6.11.0-Linux.deb"
            ),
            version="6.11.0",
            sha256="de9fa1cc4cf51c493e708a1c7608b192ad4956717b684d9c4501e22fd22d88a0",
        ),
        PrerequisiteRecord(
            name="kissat",
            source="https://github.com/arminbiere/kissat/archive/refs/tags/rel-4.0.1.tar.gz",
            version="4.0.1",
            sha256="4b41edf12ffa5f8e8b1986e5ad3e0bedb4d34b0ed3ecc7c13362bc7ba0aba66b",
        ),
    ),
    submodules=tuple(
        SubmoduleRecord(path=path, commit=commit) for path, commit in PINNED_SUBMODULES
    ),
    runtime=RuntimeIdentity(
        glibc_baseline="2.35",
        cpu_baseline="x86-64",
        dynamic_loader="/lib64/ld-linux-x86-64.so.2",
        shared_libraries=(
            "libc.so.6",
            "libgcc_s.so.1",
            "libm.so.6",
            "libstdc++.so.6",
        ),
    ),
    launcher=LauncherRecipe(
        package="kani-verifier",
        binaries=("kani", "cargo-kani"),
        archive_template="kani-{version}-launcher-{target}.tar.gz",
    ),
    attestation=AttestationRecipe(
        verifier="gh",
        command_prefix=("gh", "attestation", "verify"),
    ),
    licences=(
        LicenceRecord(
            component="cbmc",
            licence="BSD-4-Clause",
            source="https://github.com/diffblue/cbmc/blob/develop/LICENSE",
            evidence=NOTICE_INVENTORY,
        ),
        LicenceRecord(
            component="kani",
            licence="Apache-2.0 OR MIT",
            source=(
                "https://github.com/model-checking/kani/blob/"
                f"{PINNED_UPSTREAM_COMMIT}/LICENSE-MIT"
            ),
            evidence=NOTICE_INVENTORY,
        ),
        LicenceRecord(
            component="kissat",
            licence="MIT",
            source="https://github.com/arminbiere/kissat/blob/rel-4.0.1/LICENSE",
            evidence=COMPONENT_LICENCE,
        ),
        LicenceRecord(
            component="rustc",
            licence="Apache-2.0 OR MIT (plus some BSD)",
            source="https://github.com/rust-lang/rust/blob/master/COPYRIGHT",
            evidence=NOTICE_INVENTORY,
        ),
    ),
)


def pinned_recipe() -> Recipe:
    """Return the pinned recipe after validating it.

    Returns
    -------
    Recipe
        The recipe EP-M1 established.

    Raises
    ------
    ProverToolError
        Raised when the pinned constant no longer satisfies its invariants.
    """
    validate_recipe(_PINNED)
    return _PINNED


def _document(recipe: Recipe) -> dict[str, object]:
    """Render every recipe group as JSON-compatible values."""
    upstream = recipe.upstream
    runtime = recipe.runtime
    return {
        "schema_version": recipe.schema_version,
        "upstream": {
            "repository": upstream.repository,
            "commit": upstream.commit,
            "crate_version": upstream.crate_version,
            "tag": upstream.tag,
            "published_at": upstream.published_at,
            "toolchain_channel": upstream.toolchain_channel,
            "rustc_version": upstream.rustc_version,
        },
        "target": recipe.target,
        "compiler_floor": list(recipe.compiler_floor),
        "builder": {
            "image": recipe.builder.image,
            "glibc": recipe.builder.glibc,
        },
        "prerequisites": [
            {
                "name": item.name,
                "source": item.source,
                "version": item.version,
                "sha256": item.sha256,
            }
            for item in recipe.prerequisites
        ],
        "submodules": [
            {"path": item.path, "commit": item.commit} for item in recipe.submodules
        ],
        "runtime": {
            "glibc_baseline": runtime.glibc_baseline,
            "cpu_baseline": runtime.cpu_baseline,
            "dynamic_loader": runtime.dynamic_loader,
            "shared_libraries": list(runtime.shared_libraries),
        },
        "launcher": {
            "package": recipe.launcher.package,
            "binaries": list(recipe.launcher.binaries),
            "archive_template": recipe.launcher.archive_template,
        },
        "attestation": {
            "verifier": recipe.attestation.verifier,
            "command_prefix": list(recipe.attestation.command_prefix),
        },
        "licences": [
            {
                "component": item.component,
                "licence": item.licence,
                "source": item.source,
                "evidence": item.evidence,
            }
            for item in recipe.licences
        ],
    }


def recipe_to_json(recipe: Recipe) -> bytes:
    """Render a recipe as deterministic UTF-8 JSON.

    Parameters
    ----------
    recipe : Recipe
        Recipe to render.

    Returns
    -------
    bytes
        Sorted-key JSON with a final newline.
    """
    text = json.dumps(_document(recipe), sort_keys=True, indent=2, ensure_ascii=True)
    return (text + "\n").encode("ascii")


def recipe_digest(recipe: Recipe) -> str:
    """Return the lowercase SHA-256 of the canonical recipe bytes.

    Parameters
    ----------
    recipe : Recipe
        Recipe to digest.

    Returns
    -------
    str
        Lowercase hexadecimal digest.
    """
    return hashlib.sha256(recipe_to_json(recipe)).hexdigest()
