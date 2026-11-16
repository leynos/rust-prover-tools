"""Strict parsing of a serialised snapshot build recipe.

The parser is the inverse of `recipe.recipe_to_json` and accepts nothing else.
It refuses duplicate and unknown keys, refuses coercion between strings,
numbers, booleans and null, and validates the assembled model before returning
it, so a hand-edited recipe cannot reach a build with a partly trusted shape.
"""

from __future__ import annotations

import typing as typ

from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_snapshot.models import (
    PrerequisiteRecord,
    RuntimeIdentity,
    SubmoduleRecord,
)
from rust_prover_tools.kani_snapshot.policy import reject_unknown_keys
from rust_prover_tools.kani_snapshot.producer.recipe_model import (
    RECIPE_SCHEMA_VERSION,
    AttestationRecipe,
    BuilderRecord,
    LauncherRecipe,
    LicenceRecord,
    Recipe,
    UpstreamIdentity,
)
from rust_prover_tools.kani_snapshot.producer.recipe_rules import validate_recipe
from rust_prover_tools.kani_snapshot.strict_json import strict_json_loads
from rust_prover_tools.kani_snapshot.validation import (
    require_int,
    require_mapping,
    require_sequence,
    require_sha256,
    require_truthy_text,
    validate_target,
)

__all__ = ("RECIPE_KEYS", "parse_recipe")

#: A compiler floor names a major and a minor release.
_FLOOR_COMPONENTS: typ.Final[int] = 2

#: Exact top-level key set accepted in recipe schema version 1.
RECIPE_KEYS: typ.Final[frozenset[str]] = frozenset({
    "attestation",
    "builder",
    "compiler_floor",
    "launcher",
    "licences",
    "prerequisites",
    "runtime",
    "schema_version",
    "submodules",
    "target",
    "upstream",
})

_UPSTREAM_KEYS: typ.Final[frozenset[str]] = frozenset({
    "commit",
    "crate_version",
    "published_at",
    "repository",
    "rustc_version",
    "tag",
    "toolchain_channel",
})
_BUILDER_KEYS: typ.Final[frozenset[str]] = frozenset({"glibc", "image"})
_RUNTIME_KEYS: typ.Final[frozenset[str]] = frozenset({
    "cpu_baseline",
    "dynamic_loader",
    "glibc_baseline",
    "shared_libraries",
})
_LAUNCHER_KEYS: typ.Final[frozenset[str]] = frozenset({
    "archive_template",
    "binaries",
    "package",
})
_ATTESTATION_KEYS: typ.Final[frozenset[str]] = frozenset({
    "command_prefix",
    "verifier",
})
_PREREQUISITE_KEYS: typ.Final[frozenset[str]] = frozenset({
    "name",
    "sha256",
    "source",
    "version",
})
_SUBMODULE_KEYS: typ.Final[frozenset[str]] = frozenset({"commit", "path"})
_LICENCE_KEYS: typ.Final[frozenset[str]] = frozenset({
    "component",
    "evidence",
    "licence",
    "source",
})


def _group(value: object, field: str, keys: frozenset[str]) -> dict[str, object]:
    """Return an object with exactly the keys its schema allows."""
    document = require_mapping(value, f"recipe {field}")
    reject_unknown_keys(document, keys, f"recipe {field}")
    missing = sorted(keys - set(document))
    if missing:
        msg = f"snapshot recipe {field} is missing keys: {', '.join(missing)}"
        raise ProverToolError(msg)
    return document


def _items(value: object, field: str, keys: frozenset[str]) -> list[dict[str, object]]:
    """Return an array whose members each carry exactly `keys`."""
    return [
        _group(item, f"{field}[{index}]", keys)
        for index, item in enumerate(require_sequence(value, f"recipe {field}"))
    ]


def _optional_text(value: object, field: str) -> str | None:
    """Return `None` or a checked string."""
    return None if value is None else require_truthy_text(value, field)


def _texts(value: object, field: str) -> tuple[str, ...]:
    """Return a tuple of checked strings from a JSON array."""
    return tuple(
        require_truthy_text(item, f"{field}[{index}]")
        for index, item in enumerate(require_sequence(value, field))
    )


def _schema_version(value: object) -> int:
    """Require the single supported recipe schema version."""
    version = require_int(value, "recipe schema_version")
    if version != RECIPE_SCHEMA_VERSION:
        msg = f"snapshot recipe schema_version must be {RECIPE_SCHEMA_VERSION}"
        raise ProverToolError(msg)
    return version


def _upstream(value: object) -> UpstreamIdentity:
    """Parse the upstream identity group."""
    document = _group(value, "upstream", _UPSTREAM_KEYS)
    return UpstreamIdentity(
        repository=require_truthy_text(
            document["repository"], "recipe upstream repository"
        ),
        commit=require_truthy_text(document["commit"], "recipe upstream commit"),
        crate_version=require_truthy_text(
            document["crate_version"], "recipe upstream crate_version"
        ),
        tag=require_truthy_text(document["tag"], "recipe upstream tag"),
        published_at=require_truthy_text(
            document["published_at"], "recipe upstream published_at"
        ),
        toolchain_channel=require_truthy_text(
            document["toolchain_channel"], "recipe upstream toolchain_channel"
        ),
        rustc_version=require_truthy_text(
            document["rustc_version"], "recipe upstream rustc_version"
        ),
    )


def _floor(value: object) -> tuple[int, int]:
    """Parse the compiler floor as a two-element integer array."""
    parts = require_sequence(value, "recipe compiler_floor")
    if len(parts) != _FLOOR_COMPONENTS:
        msg = "snapshot recipe compiler_floor must hold a major and minor version"
        raise ProverToolError(msg)
    return (
        require_int(parts[0], "recipe compiler_floor[0]"),
        require_int(parts[1], "recipe compiler_floor[1]"),
    )


def _builder(value: object) -> BuilderRecord:
    """Parse the builder image group."""
    document = _group(value, "builder", _BUILDER_KEYS)
    return BuilderRecord(
        image=require_truthy_text(document["image"], "recipe builder image"),
        glibc=require_truthy_text(document["glibc"], "recipe builder glibc"),
    )


def _prerequisites(value: object) -> tuple[PrerequisiteRecord, ...]:
    """Parse the prerequisite records."""
    return tuple(
        PrerequisiteRecord(
            name=require_truthy_text(item["name"], "recipe prerequisite name"),
            source=require_truthy_text(item["source"], "recipe prerequisite source"),
            version=require_truthy_text(item["version"], "recipe prerequisite version"),
            sha256=require_sha256(item["sha256"], "recipe prerequisite sha256"),
        )
        for item in _items(value, "prerequisites", _PREREQUISITE_KEYS)
    )


def _submodules(value: object) -> tuple[SubmoduleRecord, ...]:
    """Parse the submodule gitlink records."""
    return tuple(
        SubmoduleRecord(
            path=require_truthy_text(item["path"], "recipe submodule path"),
            commit=require_truthy_text(item["commit"], "recipe submodule commit"),
        )
        for item in _items(value, "submodules", _SUBMODULE_KEYS)
    )


def _runtime(value: object) -> RuntimeIdentity:
    """Parse the measured runtime group."""
    document = _group(value, "runtime", _RUNTIME_KEYS)
    return RuntimeIdentity(
        glibc_baseline=require_truthy_text(
            document["glibc_baseline"], "recipe runtime glibc_baseline"
        ),
        cpu_baseline=_optional_text(
            document["cpu_baseline"], "recipe runtime cpu_baseline"
        ),
        dynamic_loader=_optional_text(
            document["dynamic_loader"], "recipe runtime dynamic_loader"
        ),
        shared_libraries=_texts(
            document["shared_libraries"], "recipe runtime shared_libraries"
        ),
    )


def _launcher(value: object) -> LauncherRecipe:
    """Parse the launcher group."""
    document = _group(value, "launcher", _LAUNCHER_KEYS)
    return LauncherRecipe(
        package=require_truthy_text(document["package"], "recipe launcher package"),
        binaries=_texts(document["binaries"], "recipe launcher binaries"),
        archive_template=require_truthy_text(
            document["archive_template"], "recipe launcher archive_template"
        ),
    )


def _attestation(value: object) -> AttestationRecipe:
    """Parse the attestation group."""
    document = _group(value, "attestation", _ATTESTATION_KEYS)
    return AttestationRecipe(
        verifier=require_truthy_text(
            document["verifier"], "recipe attestation verifier"
        ),
        command_prefix=_texts(
            document["command_prefix"], "recipe attestation command_prefix"
        ),
    )


def _licences(value: object) -> tuple[LicenceRecord, ...]:
    """Parse the licence evidence records."""
    return tuple(
        LicenceRecord(
            component=require_truthy_text(
                item["component"], "recipe licence component"
            ),
            licence=require_truthy_text(item["licence"], "recipe licence"),
            source=require_truthy_text(item["source"], "recipe licence source"),
            evidence=require_truthy_text(item["evidence"], "recipe licence evidence"),
        )
        for item in _items(value, "licences", _LICENCE_KEYS)
    )


def parse_recipe(data: bytes) -> Recipe:
    """Parse exact recipe bytes into a validated `Recipe`.

    Parameters
    ----------
    data : bytes
        Exact canonical JSON bytes.

    Returns
    -------
    Recipe
        The validated recipe.

    Raises
    ------
    ProverToolError
        Raised for invalid UTF-8, JSON syntax, duplicate, unknown or missing
        keys, a wrong type, an unsupported schema version, or a model that
        fails `validate_recipe`.
    """
    document = require_mapping(strict_json_loads(data, "recipe"), "recipe")
    reject_unknown_keys(document, RECIPE_KEYS, "recipe")
    missing = sorted(RECIPE_KEYS - set(document))
    if missing:
        msg = f"snapshot recipe is missing keys: {', '.join(missing)}"
        raise ProverToolError(msg)
    recipe = Recipe(
        schema_version=_schema_version(document["schema_version"]),
        upstream=_upstream(document["upstream"]),
        target=validate_target(document["target"]),
        compiler_floor=_floor(document["compiler_floor"]),
        builder=_builder(document["builder"]),
        prerequisites=_prerequisites(document["prerequisites"]),
        submodules=_submodules(document["submodules"]),
        runtime=_runtime(document["runtime"]),
        launcher=_launcher(document["launcher"]),
        attestation=_attestation(document["attestation"]),
        licences=_licences(document["licences"]),
    )
    validate_recipe(recipe)
    return recipe
