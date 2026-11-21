"""Which build inputs decide whether a published release may be reused.

A published release answers a later run with a no-op only when that run would
rebuild the same inputs. Those inputs are declared, not observed: the upstream
commit, this repository's build commit and the pinned recipe. Anything that
legitimately differs between runs — the run and attempt, timings, cache state,
asset sizes — must stay out of the document, or an unchanged rebuild could
never be recognised. These tests pin both halves of that contract.
"""

from __future__ import annotations

import dataclasses as dc
import re
import typing as typ

import pytest

from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_snapshot.producer.input_fingerprint import (
    FINGERPRINT_SCHEMA_VERSION,
    fingerprint_document,
    input_fingerprint,
)
from rust_prover_tools.kani_snapshot.producer.recipe import pinned_recipe
from rust_prover_tools.unittests import kani_producer_fixtures as fixtures

if typ.TYPE_CHECKING:
    from rust_prover_tools.kani_snapshot.producer.recipe_model import Recipe

PINNED = pinned_recipe()
_SHA256 = re.compile(r"^[0-9a-f]{64}$")

#: Names that would make the fingerprint a fact about one run, not the inputs.
_RUN_OBSERVATIONS = (
    "run_id",
    "run_attempt",
    "wall_seconds",
    "disk_high_water",
    "cache",
    "size",
    "started_at",
)


def _fingerprint(
    *,
    upstream_commit: str = fixtures.UPSTREAM_COMMIT,
    builder_commit: str = fixtures.BUILDER_COMMIT,
    recipe: Recipe = PINNED,
) -> str:
    """Return the fingerprint for the pinned inputs with any override."""
    return input_fingerprint(
        upstream_commit=upstream_commit,
        builder_commit=builder_commit,
        recipe=recipe,
    )


def test_the_fingerprint_is_a_lowercase_sha256() -> None:
    """The value is compared across runs, so its shape is fixed."""
    assert _SHA256.match(_fingerprint())


def test_the_same_inputs_produce_the_same_fingerprint() -> None:
    """An unchanged rebuild must be recognisable as one."""
    assert _fingerprint() == _fingerprint()


def test_the_builder_commit_changes_the_fingerprint() -> None:
    """The builder tree pins both lockfiles and the build configuration."""
    assert _fingerprint(builder_commit="c" * 40) != _fingerprint()


def test_a_recipe_change_changes_the_fingerprint() -> None:
    """A prerequisite digest is a build input even when no commit moved."""
    changed = dc.replace(
        PINNED,
        builder=dc.replace(PINNED.builder, image="example.invalid/other:1"),
    )
    assert _fingerprint(recipe=changed) != _fingerprint()


def test_another_upstream_commit_is_not_this_recipe() -> None:
    """The recipe describes one source tree, so a third commit is an error."""
    with pytest.raises(ProverToolError, match="not the pinned recipe commit"):
        _fingerprint(upstream_commit="a" * 40)


def test_an_abbreviated_builder_commit_is_refused() -> None:
    """An abbreviation could collide between two different trees."""
    with pytest.raises(ProverToolError, match="builder"):
        _fingerprint(builder_commit="abc1234")


def test_run_observations_are_not_part_of_the_inputs() -> None:
    """A rerun of the same inputs must reuse the same release."""
    document = fingerprint_document(
        upstream_commit=fixtures.UPSTREAM_COMMIT,
        builder_commit=fixtures.BUILDER_COMMIT,
        recipe=PINNED,
    )
    rendered = repr(document)
    for name in _RUN_OBSERVATIONS:
        assert name not in rendered, f"{name} leaked into the fingerprint"


def test_the_document_is_versioned() -> None:
    """A change to what counts as an input needs a new version."""
    document = fingerprint_document(
        upstream_commit=fixtures.UPSTREAM_COMMIT,
        builder_commit=fixtures.BUILDER_COMMIT,
        recipe=PINNED,
    )
    assert document["schema_version"] == FINGERPRINT_SCHEMA_VERSION
