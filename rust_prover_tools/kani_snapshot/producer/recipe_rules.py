"""Invariants a pinned snapshot build recipe must satisfy.

`pinned_recipe` and `parse_recipe` are the only validated entry points, and
both call `validate_recipe`, the sole gate they use. The frozen records are
plain dataclasses, so constructing one directly bypasses this module; the gate
guards the entry points, not the constructors. It fails closed on every value
that could make the build unreproducible, including a compiler floor the
pinned toolchain cannot reach and a runtime baseline the builder image did not
exercise.
"""

from __future__ import annotations

import re
import string
import typing as typ

from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_snapshot.policy import is_safe_asset_name
from rust_prover_tools.kani_snapshot.producer.compiler_floor import (
    COMPILER_FLOOR,
    parse_rustc_version,
)
from rust_prover_tools.kani_snapshot.producer.recipe_evidence import (
    PINNED_SUBMODULES,
    PINNED_UPSTREAM_COMMIT,
)
from rust_prover_tools.kani_snapshot.producer.recipe_model import (
    ATTESTATION_VERIFIER,
    COMPONENT_LICENCE,
    LAUNCHER_BINARIES,
    NOTICE_INVENTORY,
    RECIPE_SCHEMA_VERSION,
    REQUIRED_LICENCE_COMPONENTS,
    REQUIRED_PREREQUISITES,
    AttestationRecipe,
    LauncherRecipe,
    LicenceRecord,
    Recipe,
    UpstreamIdentity,
)

if typ.TYPE_CHECKING:
    from rust_prover_tools.kani_snapshot.models import (
        PrerequisiteRecord,
        RuntimeIdentity,
    )
from rust_prover_tools.kani_snapshot.validation import (
    FULL_SHA_PATTERN,
    SHA256_PATTERN,
    is_contained_relative_path,
    require_truthy_text,
    validate_target,
)

__all__ = ("validate_recipe",)

_CRATE_VERSION: typ.Final[re.Pattern[str]] = re.compile(r"\d+\.\d+\.\d+")
_TOOLCHAIN_CHANNEL: typ.Final[re.Pattern[str]] = re.compile(
    r"nightly-\d{4}-\d{2}-\d{2}"
)
_PUBLISHED_AT: typ.Final[re.Pattern[str]] = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z"
)
_GLIBC_VERSION: typ.Final[re.Pattern[str]] = re.compile(r"\d+\.\d+")
_IMAGE_NAME: typ.Final[re.Pattern[str]] = re.compile(r"[a-z0-9][a-z0-9._-]*")
_CARGO_PACKAGE: typ.Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9_-]+")
#: The only fields a launcher archive template may substitute.
_LAUNCHER_TEMPLATE_FIELDS: typ.Final[frozenset[str]] = frozenset({"version", "target"})
_LIBRARY_NAME: typ.Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9_.+-]+")

#: A compiler floor names a major and a minor release.
_FLOOR_COMPONENTS: typ.Final[int] = 2


def _fail(message: str) -> typ.NoReturn:
    """Raise one user-facing recipe validation failure."""
    raise ProverToolError(message)


def _require_version(value: str, field: str) -> str:
    """Require a plain three-part version, which never reaches a shell."""
    if _CRATE_VERSION.fullmatch(value) is None:
        _fail(f"snapshot recipe {field} must be a plain semver such as 0.68.0")
    return value


def _glibc_tuple(value: str) -> tuple[int, int]:
    """Return a glibc major/minor pair for comparison."""
    major, minor = value.split(".", 1)
    return int(major), int(minor)


def _validate_upstream(upstream: UpstreamIdentity) -> None:
    """Check the release identity and the compiler it pins."""
    if FULL_SHA_PATTERN.fullmatch(upstream.commit) is None:
        _fail("snapshot recipe upstream commit must be a full lowercase 40-hex commit")
    if not upstream.repository.strip():
        _fail("snapshot recipe upstream repository must not be empty")
    _require_version(upstream.crate_version, "upstream crate_version")
    if upstream.tag != f"kani-{upstream.crate_version}":
        _fail("snapshot recipe upstream tag must be kani-<crate_version>")
    if _PUBLISHED_AT.fullmatch(upstream.published_at) is None:
        _fail("snapshot recipe upstream published_at must be ISO-8601 UTC")
    if _TOOLCHAIN_CHANNEL.fullmatch(upstream.toolchain_channel) is None:
        _fail(
            "snapshot recipe upstream toolchain_channel must be a dated nightly "
            "such as nightly-2026-08-21"
        )
    require_truthy_text(upstream.rustc_version, "recipe upstream rustc_version")


def _validate_floor(recipe: Recipe) -> None:
    """Check that the promised compiler floor is reachable and credible."""
    floor = recipe.compiler_floor
    if len(floor) != _FLOOR_COMPONENTS or any(
        not _is_plain_int(part) for part in floor
    ):
        _fail("snapshot recipe compiler floor must be a major/minor pair")
    if tuple(floor) < COMPILER_FLOOR:
        _fail(
            f"snapshot recipe compiler floor must be at least "
            f"{COMPILER_FLOOR[0]}.{COMPILER_FLOOR[1]}"
        )
    pinned = parse_rustc_version(recipe.upstream.rustc_version)
    if tuple(floor) > pinned:
        _fail(
            f"snapshot recipe compiler floor {floor[0]}.{floor[1]} is newer than "
            f"the pinned compiler {pinned[0]}.{pinned[1]}"
        )


def _validate_builder(recipe: Recipe) -> None:
    """Check that the builder image can support the claimed runtime floor."""
    builder = recipe.builder
    if _IMAGE_NAME.fullmatch(builder.image) is None:
        _fail("snapshot recipe builder image must be a plain image name")
    if _GLIBC_VERSION.fullmatch(builder.glibc) is None:
        _fail("snapshot recipe builder glibc must be a major/minor pair")
    baseline = _glibc_tuple(recipe.runtime.glibc_baseline)
    linking_image = _glibc_tuple(builder.glibc)
    if baseline > linking_image:
        _fail(
            f"snapshot recipe runtime glibc_baseline "
            f"{recipe.runtime.glibc_baseline} is newer than the builder glibc "
            f"{builder.glibc}"
        )
    if baseline < linking_image:
        _fail(
            f"snapshot recipe runtime glibc_baseline "
            f"{recipe.runtime.glibc_baseline} is older than the builder glibc "
            f"{builder.glibc}, the only baseline the build actually exercised"
        )


def _validate_prerequisites(prerequisites: tuple[PrerequisiteRecord, ...]) -> None:
    """Check that every prerequisite names a trusted, digested artifact."""
    names = [record.name for record in prerequisites]
    missing = sorted(set(REQUIRED_PREREQUISITES) - set(names))
    if missing:
        _fail(f"snapshot recipe prerequisites are missing: {', '.join(missing)}")
    duplicated = sorted({name for name in names if names.count(name) > 1})
    if duplicated:
        _fail(f"snapshot recipe prerequisites repeat: {', '.join(duplicated)}")
    for record in prerequisites:
        require_truthy_text(record.source, "recipe prerequisite source")
        require_truthy_text(record.version, "recipe prerequisite version")
        if SHA256_PATTERN.fullmatch(record.sha256) is None:
            _fail(
                f"snapshot recipe prerequisite {record.name!r} sha256 must be "
                "a lowercase 64-hex digest"
            )


def _require_pinned_gitlinks(recipe: Recipe) -> None:
    """Require the pinned commit's recipe to record the pinned tree exactly.

    Only the pinned commit has recorded evidence, so only its documents are
    checked this way. A recipe for any other commit is still checked for
    ordering, duplication and containment, but its tree has to be evidenced by
    the caller that chose it.
    """
    if recipe.upstream.commit != PINNED_UPSTREAM_COMMIT:
        return
    recorded = tuple((record.path, record.commit) for record in recipe.submodules)
    if recorded != PINNED_SUBMODULES:
        expected = ", ".join(path for path, _ in PINNED_SUBMODULES)
        _fail(
            "snapshot recipe submodules must record the pinned release's "
            f"gitlinks exactly: {expected}"
        )


def _validate_submodules(recipe: Recipe) -> None:
    """Check that recorded gitlinks are complete and cannot escape the tree.

    A non-empty, sorted list is still not the pinned release's tree, so a recipe
    naming the pinned commit must record exactly the gitlinks `recipe_evidence`
    recorded for it. Without that comparison a document could silently drop a
    submodule and build a tree nobody inspected.
    """
    submodules = recipe.submodules
    if not submodules:
        _fail("snapshot recipe must record the upstream release's submodules")
    paths = [record.path for record in submodules]
    if paths != sorted(paths):
        _fail("snapshot recipe submodules must be sorted by path")
    duplicated = sorted({path for path in paths if paths.count(path) > 1})
    if duplicated:
        _fail(f"snapshot recipe submodules repeat: {', '.join(duplicated)}")
    _require_pinned_gitlinks(recipe)
    for record in submodules:
        if not is_contained_relative_path(record.path):
            _fail(
                f"snapshot recipe submodule path {record.path!r} must stay below "
                "the repository root"
            )
        if FULL_SHA_PATTERN.fullmatch(record.commit) is None:
            _fail(
                f"snapshot recipe submodule {record.path!r} commit must be a "
                "full lowercase 40-hex commit"
            )


def _validate_runtime(runtime: RuntimeIdentity) -> None:
    """Check the measured runtime baseline and shared library list."""
    if _GLIBC_VERSION.fullmatch(runtime.glibc_baseline) is None:
        _fail("snapshot recipe runtime glibc_baseline must be a major/minor pair")
    if runtime.dynamic_loader is not None and not runtime.dynamic_loader.startswith(
        "/"
    ):
        _fail("snapshot recipe runtime dynamic_loader must be an absolute path")
    libraries = list(runtime.shared_libraries)
    if libraries != sorted(set(libraries)):
        _fail("snapshot recipe runtime shared_libraries must be sorted and unique")
    for library in libraries:
        if _LIBRARY_NAME.fullmatch(library) is None:
            _fail(
                f"snapshot recipe runtime shared library {library!r} must be a "
                "plain library name"
            )


def _launcher_template_fields(template: str) -> frozenset[str]:
    """Return the field names a launcher archive template substitutes.

    Only plain `{version}` and `{target}` fields are acceptable. A conversion,
    a format spec or a positional field is refused here rather than left to
    `str.format`, which would raise `KeyError` or `IndexError` instead of the
    `ProverToolError` this module promises.
    """
    try:
        parsed = tuple(string.Formatter().parse(template))
    except ValueError as exc:
        _fail(f"snapshot recipe launcher archive_template is malformed: {exc}")
    fields: list[str] = []
    for _, field_name, format_spec, conversion in parsed:
        if field_name is None:
            continue
        is_plain = field_name.isidentifier() and not format_spec and not conversion
        if not is_plain:
            _fail(
                "snapshot recipe launcher archive_template must use only plain "
                "{version} and {target} fields"
            )
        fields.append(field_name)
    return frozenset(fields)


def _validate_launcher(launcher: LauncherRecipe) -> None:
    """Check that the launcher recipe names both entry points and a safe asset."""
    if _CARGO_PACKAGE.fullmatch(launcher.package) is None:
        _fail("snapshot recipe launcher package must be a Cargo package name")
    if tuple(launcher.binaries) != LAUNCHER_BINARIES:
        _fail(
            "snapshot recipe launcher binaries must be exactly "
            f"{', '.join(LAUNCHER_BINARIES)}"
        )
    template = launcher.archive_template
    if _launcher_template_fields(template) != _LAUNCHER_TEMPLATE_FIELDS:
        _fail(
            "snapshot recipe launcher archive_template must contain exactly "
            "{version} and {target}"
        )
    expanded = template.format(version="0.0.0", target="x86_64-unknown-linux-gnu")
    if not is_safe_asset_name(expanded):
        _fail("snapshot recipe launcher archive_template must expand to a basename")


def _validate_attestation(attestation: AttestationRecipe) -> None:
    """Check the verifier identity and its frozen argument prefix."""
    if attestation.verifier != ATTESTATION_VERIFIER:
        _fail(
            "snapshot recipe attestation verifier must be "
            f"{ATTESTATION_VERIFIER}, not {attestation.verifier!r}"
        )
    prefix = attestation.command_prefix
    if prefix[:3] != (ATTESTATION_VERIFIER, "attestation", "verify"):
        _fail(
            "snapshot recipe attestation command_prefix must start with "
            "'gh attestation verify'"
        )
    if any(not argument for argument in prefix):
        _fail("snapshot recipe attestation command_prefix must not hold blanks")


def _validate_licences(licences: tuple[LicenceRecord, ...]) -> None:
    """Check that every redistributed component carries licence evidence."""
    components = [record.component for record in licences]
    missing = sorted(set(REQUIRED_LICENCE_COMPONENTS) - set(components))
    if missing:
        _fail(f"snapshot recipe licences are missing: {', '.join(missing)}")
    duplicated = sorted({name for name in components if components.count(name) > 1})
    if duplicated:
        _fail(f"snapshot recipe licences repeat: {', '.join(duplicated)}")
    for record in licences:
        require_truthy_text(record.licence, "recipe licence")
        require_truthy_text(record.source, "recipe licence source")
        if record.evidence not in {NOTICE_INVENTORY, COMPONENT_LICENCE}:
            _fail(
                f"snapshot recipe licence {record.component!r} evidence must be "
                f"{NOTICE_INVENTORY} or {COMPONENT_LICENCE}"
            )


def _is_plain_int(value: object) -> bool:
    """Return whether a value is an integer rather than a boolean.

    `True == 1` in Python, so `isinstance(value, int)` alone accepts a boolean
    that the strict parser then refuses, and a recipe validated here would not
    survive the round trip.
    """
    return isinstance(value, int) and not isinstance(value, bool)


def _validate_schema(recipe: Recipe) -> None:
    """Check the recipe names a schema this implementation understands."""
    # `True == 1` in Python, so a bare inequality would accept the boolean and
    # accept a recipe the strict parser then refuses, breaking the round trip.
    if not _is_plain_int(recipe.schema_version):
        _fail("snapshot recipe schema_version must be an integer")
    if recipe.schema_version != RECIPE_SCHEMA_VERSION:
        _fail(
            f"snapshot recipe schema_version must be {RECIPE_SCHEMA_VERSION}, "
            f"got {recipe.schema_version}"
        )


def validate_recipe(recipe: Recipe) -> None:
    """Validate every invariant a pinned recipe must satisfy.

    Parameters
    ----------
    recipe : Recipe
        Recipe to validate.

    Raises
    ------
    ProverToolError
        Raised for an unsupported schema version, a malformed identity, a
        runtime baseline that differs from the builder glibc, a missing or
        duplicated prerequisite or licence, an unreachable compiler floor, an
        unsafe asset template or an unexpected verifier.
    """
    _validate_schema(recipe)
    _validate_upstream(recipe.upstream)
    validate_target(recipe.target)
    _validate_floor(recipe)
    _validate_prerequisites(recipe.prerequisites)
    _validate_submodules(recipe)
    _validate_runtime(recipe.runtime)
    _validate_builder(recipe)
    _validate_launcher(recipe.launcher)
    _validate_attestation(recipe.attestation)
    _validate_licences(recipe.licences)
