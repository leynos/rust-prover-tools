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
    assert environment["UPSTREAM_REPOSITORY"] == PINNED.upstream.repository, (
        "the UPSTREAM_REPOSITORY environment must match the recipe"
    )
    assert job(workflow, "build")["runs-on"] == PINNED.builder.image, (
        "the build job's runs-on must name the recipe's builder image"
    )


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
    assert source in declared, (
        "the build must download the prerequisite from its pinned source"
    )
    assert digest in declared, (
        "the build must fetch the prerequisite at its pinned digest"
    )


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
    assert 'uv --directory "$GITHUB_WORKSPACE/builder"' in evidence, (
        "the evidence measurement must run uv from the workspace-absolute builder"
    )
    assert "--directory builder" not in evidence, (
        "a workspace-relative builder path would move the child's working directory"
    )
    assert '"$GITHUB_WORKSPACE/producer/evidence.json"' in evidence, (
        "the evidence must be written to the workspace-absolute producer path"
    )
    assert '"$GITHUB_WORKSPACE/producer/plan.json"' in evidence, (
        "the plan must be read from the workspace-absolute producer path"
    )


def test_the_source_state_is_compared_before_and_after_the_build(
    workflow: dict[str, object],
) -> None:
    """A build that rewrites its own input is not evidence about that input."""
    build = named_scripts(job(workflow, "build"))["Build the upstream payloads"]
    assert "source-before.txt" in build, (
        "the build must record the source state before running the plan"
    )
    assert "source-after.txt" in build, (
        "the build must record the source state after running the plan"
    )
    assert "--untracked-files=no" in build, (
        "the source comparison must ignore untracked files"
    )
    assert "the build changed the checked-out source tree" in build, (
        "a build that rewrites its own input must fail with that message"
    )
    assert "submodule status --recursive" in build, (
        "the source comparison must cover the checked-out submodules"
    )
    assert 'test "$head" = "$UPSTREAM_COMMIT"' in build, (
        "the checkout's HEAD is not compared with the resolved commit"
    )
    assert "the checked-out source tree is not clean" in build, (
        "a tree that was already dirty would satisfy the before/after diff"
    )
    assert "submodule is not at its recorded commit" in build, (
        "a submodule off its recorded commit must fail the build"
    )
    assert 'cp "$RUNNER_TEMP/source-before.txt"' in build, (
        "the observed source state is not preserved in the build artefact"
    )


def test_the_launcher_payload_carries_the_consumer_archive_root(
    workflow: dict[str, object],
) -> None:
    """The consumer strips one root, so the archive must supply `kani-<v>`."""
    scripts = named_scripts(job(workflow, "build"))
    packing = scripts["Assemble the release payloads"]
    assert 'bundle_root="kani-${version}"' in packing, (
        "the launch archive root must be kani-<version>"
    )
    assert 'launcher_root="$RUNNER_TEMP/launcher-root/$bundle_root"' in packing, (
        "the launcher tree must be staged under the archive root"
    )
    assert 'mkdir -p "$launcher_root/bin"' in packing, (
        "the launcher archive must include a bin directory"
    )
    assert (
        '-czf "payloads/$launcher_asset" -C "$RUNNER_TEMP/launcher-root" '
        '"$bundle_root"' in packing
    ), "the launcher archive must be packed from the staged root"
    assert '--tree "$RUNNER_TEMP/bundle-tree/$bundle_root"' in packing, (
        "the bundle tree must be staged under the archive root"
    )
    assert 'launcher_prefix="upstream/target/launcher-prefix"' in packing, (
        "the launchers are not taken from the plan's locked install"
    )
    assert 'test -x "$launcher_prefix/bin/$binary"' in packing, (
        "a missing installed launcher would be packed silently"
    )
    assert "for binary in kani cargo-kani" in packing, (
        "the launcher archive must include both kani and cargo-kani"
    )


def test_the_bundle_is_read_from_the_upstream_workspace_root(
    workflow: dict[str, object],
) -> None:
    """`cargo bundle` writes into its working directory, not `target/`."""
    packing = named_scripts(job(workflow, "build"))["Assemble the release payloads"]
    assert 'bundle="upstream/$bundle_asset"' in packing, (
        "the bundle must be read from the upstream workspace root"
    )
    assert 'test -f "$bundle"' in packing, "a missing bundle must fail the packing step"
    assert "the build produced no" in packing, (
        "a missing bundle asset must be reported by the packing step"
    )


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
    assert "producer/source-before.txt" in paths, (
        "the build artefact must preserve the pre-build source state"
    )
    assert "producer/source-after.txt" in paths, (
        "the build artefact must preserve the post-build source state"
    )


def test_a_failed_build_preserves_its_observations(
    workflow: dict[str, object],
) -> None:
    """The run whose evidence matters most is the one that failed.

    The success-path upload runs only after every earlier step passed, so a
    build that failed halfway would leave nothing behind. The failure artefact
    carries the same trees under a distinct name, so no consumer can mistake a
    partial observation set for a publishable build.
    """
    failure = [
        step
        for step in steps(job(workflow, "build"))
        if step.get("name") == "Preserve the build observations on failure"
    ]
    assert len(failure) == 1, (
        "the build must declare exactly one failure-observation upload"
    )
    assert failure[0]["if"] == "failure()", "a failed build always uploads"
    with_ = as_mapping(failure[0]["with"], "failure upload inputs")
    assert with_["name"] == (
        "kani-snapshot-build-failure-${{ github.run_id }}-${{ github.run_attempt }}"
    ), "the failure artefact must use the run-scoped failure name"
    paths = str(with_["path"]).splitlines()
    assert "producer/" in paths, (
        "the failure artefact must preserve the producer observations"
    )
    assert "payloads/" in paths, "the failure artefact must preserve any built payloads"
    assert with_["if-no-files-found"] == "ignore", (
        "a run that failed before producing anything still reports its status"
    )


def test_a_failed_build_still_records_what_it_measured(
    workflow: dict[str, object],
) -> None:
    """A failed build must not discard the numbers that explain the failure.

    The plan commands are the only step where the wall time, disk high-water
    mark and final source state exist. Letting `set -e` end the script at the
    first failing command would lose all three, leaving the failure artefact
    and the run summary empty for exactly the run that needs them. The failure
    is re-raised afterwards, so capture cannot turn into acceptance.
    """
    build = named_scripts(job(workflow, "build"))["Build the upstream payloads"]
    assert 'plan-commands.sh" || status=$?' in build, (
        "the planned commands' status must be captured, not terminate the script"
    )
    assert "wall_seconds=" in build, (
        "the build must measure wall seconds before the plan can fail"
    )
    assert "disk_high_water_bytes=" in build, (
        "the build must measure disk high water before the plan can fail"
    )
    assert "producer/resources.json" in build, (
        "the build must write its observations to producer/resources.json"
    )
    assert 'source_state > "$RUNNER_TEMP/source-after.txt"' in build, (
        "the build must record the final source state before re-raising"
    )

    rethrow = build.index('exit "$status"')
    assert build.index("producer/resources.json") < rethrow, (
        "the resources must be written before the failure is re-raised"
    )
    assert build.index('source_state > "$RUNNER_TEMP/source-after.txt"') < rethrow, (
        "the final source state must be written before the failure is re-raised"
    )


def test_the_build_lets_the_producer_enumerate_the_shared_libraries(
    workflow: dict[str, object],
) -> None:
    """The measured library set and the consumer's check come from one place.

    A `--library` list built in YAML would drift from the set the consumer
    requires, so the measurement walks the sysroot itself and the workflow
    must not pass a hand-built subset.
    """
    evidence = named_scripts(job(workflow, "build"))["Measure the build evidence"]
    assert "--library" not in evidence, (
        "the measure step must not pass a hand-built library list"
    )
    assert "libstd-*.so" not in evidence, (
        "the measure step must let the producer enumerate the sysroot libraries"
    )


def test_the_assembly_input_is_read_from_the_build_artefact(
    workflow: dict[str, object],
) -> None:
    """The upload keeps the producer tree, so the input lands one level down."""
    upload = as_mapping(
        next(
            step["with"]
            for step in steps(job(workflow, "build"))
            if step.get("name") == "Upload the payloads and evidence"
        ),
        "upload inputs",
    )
    paths = str(upload["path"]).splitlines()
    assert paths == [
        "payloads/",
        "producer/assembly-input.json",
        "producer/evidence.json",
        "producer/plan.json",
        "producer/resources.json",
        "producer/source-before.txt",
        "producer/source-after.txt",
    ], "the success upload must publish the producer and payload paths exactly"

    assemble = named_scripts(job(workflow, "assemble"))
    assert (
        "--input build/producer/assembly-input.json"
        in assemble["Assemble the manifest and checksums"]
    ), "the assemble job must read the input from the downloaded build artefact"
