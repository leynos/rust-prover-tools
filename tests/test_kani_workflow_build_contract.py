"""Contract tests for the build job of the guarded snapshot workflow.

The build job is the least trusted job and the only one that executes upstream
code. It checks out the pinned commit, runs the plan's own commands, and must
hand assembly exactly the inputs and payloads it observed. Each test below pins
one of those hand-offs: the pinned solver digests, the bundle's real location,
the source state the build was allowed to consume, and the launcher archive the
consumer extracts.

Values that belong to the recipe are compared with the recipe itself rather than
repeated, so a recipe bump fails here instead of in a monthly run.
"""

from __future__ import annotations

import typing as typ

import pytest

from rust_prover_tools.kani_snapshot.producer.recipe import pinned_recipe
from tests.workflow_contracts.strict_workflow import (
    as_mapping,
    declared_text,
    job,
    named_scripts,
    read_workflow,
    steps,
)

if typ.TYPE_CHECKING:
    from rust_prover_tools.kani_snapshot.models import PrerequisiteRecord

PINNED = pinned_recipe()


@pytest.fixture(scope="module")
def workflow() -> dict[str, object]:
    """Return the parsed workflow shared by the whole module."""
    return read_workflow()


def test_the_builder_identity_comes_from_the_recipe(
    workflow: dict[str, object],
) -> None:
    """A stale pin here would build on an image the recipe does not name."""
    environment = as_mapping(workflow["env"], "workflow env")
    assert environment["UPSTREAM_REPOSITORY"] == PINNED.upstream.repository
    assert job(workflow, "build")["runs-on"] == PINNED.builder.image


@pytest.mark.parametrize(
    "prerequisite", PINNED.prerequisites, ids=lambda item: item.name
)
def test_prerequisites_are_downloaded_at_their_pinned_digest(
    workflow: dict[str, object], prerequisite: PrerequisiteRecord
) -> None:
    """The build fetches the recipe's solver, not whatever is latest."""
    source = prerequisite.source
    digest = prerequisite.sha256
    declared = declared_text(job(workflow, "build"))
    assert source in declared
    assert digest in declared


def test_the_kissat_solver_is_generated_before_it_is_made(
    workflow: dict[str, object],
) -> None:
    """The release tarball has no Makefile, so `./configure` must run first."""
    scripts = named_scripts(job(workflow, "build"))
    solver = scripts["Install the pinned prerequisite solvers"]
    configure = solver.index("./configure")
    assert configure < solver.index("make -j"), "make ran before ./configure"
    assert "/build/kissat" in solver, "the generated binary path was not used"


def test_the_build_evidence_names_workspace_absolute_paths(
    workflow: dict[str, object],
) -> None:
    """`uv --directory` moves the child, so a relative path would miss."""
    scripts = named_scripts(job(workflow, "build"))
    evidence = scripts["Measure the build evidence"]
    assert 'uv --directory "$GITHUB_WORKSPACE/builder"' in evidence
    assert "--directory builder" not in evidence
    assert '"$GITHUB_WORKSPACE/producer/evidence.json"' in evidence
    assert '"$GITHUB_WORKSPACE/producer/plan.json"' in evidence


def test_the_source_state_is_compared_before_and_after_the_build(
    workflow: dict[str, object],
) -> None:
    """A build that rewrites its own input is not evidence about that input."""
    build = named_scripts(job(workflow, "build"))["Build the upstream payloads"]
    assert "source-before.txt" in build
    assert "source-after.txt" in build
    assert "--untracked-files=no" in build
    assert "the build changed the checked-out source tree" in build
    assert "submodule status --recursive" in build
    assert 'test "$head" = "$UPSTREAM_COMMIT"' in build, (
        "the checkout's HEAD is not compared with the resolved commit"
    )
    assert "the checked-out source tree is not clean" in build, (
        "a tree that was already dirty would satisfy the before/after diff"
    )
    assert "submodule is not at its recorded commit" in build
    assert 'cp "$RUNNER_TEMP/source-before.txt"' in build, (
        "the observed source state is not preserved in the build artefact"
    )


def test_the_launcher_payload_carries_the_consumer_archive_root(
    workflow: dict[str, object],
) -> None:
    """The consumer strips one root, so the archive must supply `kani-<v>`."""
    scripts = named_scripts(job(workflow, "build"))
    packing = scripts["Assemble the release payloads"]
    assert 'bundle_root="kani-${version}"' in packing
    assert 'launcher_root="$RUNNER_TEMP/launcher-root/$bundle_root"' in packing
    assert 'mkdir -p "$launcher_root/bin"' in packing
    assert (
        '-czf "payloads/$launcher_asset" -C "$RUNNER_TEMP/launcher-root" '
        '"$bundle_root"' in packing
    )
    assert '--tree "$RUNNER_TEMP/bundle-tree/$bundle_root"' in packing
    assert 'launcher_prefix="upstream/target/launcher-prefix"' in packing, (
        "the launchers are not taken from the plan's locked install"
    )
    assert 'test -x "$launcher_prefix/bin/$binary"' in packing, (
        "a missing installed launcher would be packed silently"
    )
    assert "for binary in kani cargo-kani" in packing


def test_the_bundle_is_read_from_the_upstream_workspace_root(
    workflow: dict[str, object],
) -> None:
    """`cargo bundle` writes into its working directory, not `target/`."""
    packing = named_scripts(job(workflow, "build"))["Assemble the release payloads"]
    assert 'bundle="upstream/$bundle_asset"' in packing
    assert 'test -f "$bundle"' in packing
    assert "the build produced no" in packing


def test_the_build_artefact_preserves_the_observed_source_state(
    workflow: dict[str, object],
) -> None:
    """A failed run's input state must be inspectable after the fact."""
    upload = as_mapping(
        next(
            step["with"]
            for step in steps(job(workflow, "build"))
            if step.get("name") == "Upload the payloads and evidence"
        ),
        "upload inputs",
    )
    paths = str(upload["path"]).splitlines()
    assert "producer/source-before.txt" in paths
    assert "producer/source-after.txt" in paths


def test_the_assembly_input_is_read_from_the_build_artefact(
    workflow: dict[str, object],
) -> None:
    """The upload keeps the producer tree, so the input lands one level down."""
    uploaded = "\n".join(
        str(value)
        for step in steps(job(workflow, "build"))
        if isinstance(step.get("with"), dict)
        for value in as_mapping(step["with"], "step inputs").values()
    )
    assert "producer/assembly-input.json" in uploaded

    assemble = named_scripts(job(workflow, "assemble"))
    assert (
        "--input build/producer/assembly-input.json"
        in assemble["Assemble the manifest and checksums"]
    )
