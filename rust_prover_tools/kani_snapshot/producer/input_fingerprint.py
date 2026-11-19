"""The declared build inputs whose equality decides identity reuse.

An immutable tag may be reused only when a new run would rebuild exactly the
same input set. Upstream HEAD alone is too weak a test: the recipe, the
prerequisites and the build configuration all decide the published bytes. This
module projects every such input into one canonical document and hashes it.

The document is deliberately comparable *before* and *after* the build. The
resolve job can compute it from the pinned recipe, the resolved commit and the
builder commit; the publish job recomputes it from the manifest's recorded
identities. Observations that legitimately differ between runs — the run and
attempt, timings, cache outcomes and asset sizes — are excluded, because they
are evidence about a run rather than inputs to the build.

Lockfile bytes and the source tree are bound through the two commits rather
than hashed here. `upstream_commit` content-addresses `Cargo.lock` in the
upstream checkout, and `builder_commit` content-addresses this repository's
`uv.lock`; the build job measures both and refuses a checkout that changed
while it built, so neither can drift between resolution and publication
without changing an identity in this document.
"""

from __future__ import annotations

import hashlib
import json
import typing as typ

from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_snapshot.producer.recipe import recipe_digest
from rust_prover_tools.kani_snapshot.validation import (
    require_full_sha,
    require_truthy_text,
)

if typ.TYPE_CHECKING:
    from rust_prover_tools.kani_snapshot.producer.recipe_model import Recipe

__all__ = ("FINGERPRINT_SCHEMA_VERSION", "input_fingerprint")

#: The only fingerprint document version this implementation understands.
FINGERPRINT_SCHEMA_VERSION: typ.Final[int] = 1


def _upstream_document(commit: str, recipe: Recipe) -> dict[str, object]:
    """Render the source identity the recipe pins."""
    return {
        "commit": commit,
        "crate_version": recipe.upstream.crate_version,
        "tag": recipe.upstream.tag,
    }


def _toolchain_document(recipe: Recipe) -> dict[str, object]:
    """Render the toolchain inputs the recipe pins."""
    return {
        "channel": recipe.upstream.toolchain_channel,
        "rustc_version": recipe.upstream.rustc_version,
    }


def _runtime_document(recipe: Recipe) -> dict[str, object]:
    """Render the runtime baseline the payloads promise."""
    runtime = recipe.runtime
    return {
        "glibc_baseline": runtime.glibc_baseline,
        "cpu_baseline": runtime.cpu_baseline,
        "dynamic_loader": runtime.dynamic_loader,
        "shared_libraries": list(runtime.shared_libraries),
    }


def _submodule_documents(recipe: Recipe) -> list[dict[str, str]]:
    """Render the recursive submodule gitlinks the recipe pins."""
    return [
        {"path": record.path, "commit": record.commit} for record in recipe.submodules
    ]


def _prerequisite_documents(recipe: Recipe) -> list[dict[str, str]]:
    """Render every build prerequisite with its artifact digest."""
    return [
        {
            "name": record.name,
            "source": record.source,
            "version": record.version,
            "sha256": record.sha256,
        }
        for record in recipe.prerequisites
    ]


def fingerprint_document(
    *,
    upstream_commit: str,
    builder_commit: str,
    recipe: Recipe,
) -> dict[str, object]:
    """Return the canonical document every input contributes to.

    Parameters
    ----------
    upstream_commit : str
        Full upstream commit the snapshot is built from. It must be the commit
        the pinned recipe names, because the recipe describes that source.
    builder_commit : str
        Full builder commit whose tree pins the lockfiles and configuration.
    recipe : Recipe
        Pinned recipe supplying the remaining inputs.

    Returns
    -------
    dict[str, object]
        JSON-compatible inputs, with no run, timing, cache or size
        observation.

    Raises
    ------
    ProverToolError
        Raised when a commit is malformed or the recipe describes another
        upstream commit.
    """
    commit = require_full_sha(upstream_commit, "fingerprint upstream commit")
    if commit != recipe.upstream.commit:
        msg = (
            f"fingerprint upstream commit {commit} is not the pinned recipe "
            f"commit {recipe.upstream.commit}"
        )
        raise ProverToolError(msg)
    return {
        "schema_version": FINGERPRINT_SCHEMA_VERSION,
        "repository": recipe.upstream.repository,
        "upstream": _upstream_document(commit, recipe),
        "builder_commit": require_full_sha(builder_commit, "fingerprint builder"),
        "recipe_digest": recipe_digest(recipe),
        "target": require_truthy_text(recipe.target, "fingerprint target"),
        "compiler_floor": list(recipe.compiler_floor),
        "builder": {"image": recipe.builder.image, "glibc": recipe.builder.glibc},
        "toolchain": _toolchain_document(recipe),
        "launcher": {
            "package": recipe.launcher.package,
            "binaries": list(recipe.launcher.binaries),
            "archive_template": recipe.launcher.archive_template,
        },
        "submodules": _submodule_documents(recipe),
        "prerequisites": _prerequisite_documents(recipe),
        "runtime": _runtime_document(recipe),
    }


def input_fingerprint(
    *,
    upstream_commit: str,
    builder_commit: str,
    recipe: Recipe,
) -> str:
    """Return the lowercase SHA-256 of the canonical build-input document.

    Parameters
    ----------
    upstream_commit : str
        Full upstream commit the snapshot is built from.
    builder_commit : str
        Full builder commit whose tree pins the lockfiles and configuration.
    recipe : Recipe
        Pinned recipe supplying the remaining inputs.

    Returns
    -------
    str
        Lowercase SHA-256 over the key-sorted, separator-free document.
    """
    document = fingerprint_document(
        upstream_commit=upstream_commit,
        builder_commit=builder_commit,
        recipe=recipe,
    )
    canonical = json.dumps(
        document,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()
