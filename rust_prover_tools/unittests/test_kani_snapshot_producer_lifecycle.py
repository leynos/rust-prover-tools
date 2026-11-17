"""Typed lifecycle records must bind one run to one immutable snapshot.

A resolution and a build plan are the only things a later job may act on, so
each record refuses an identity this repository does not trust: another
workflow, another ref, a pull-request event, another run or attempt, an
upstream commit outside the pinned recipe, or a tag that does not carry that
commit's prefix. These tests also pin the canonical bytes, because the next job
compares them by digest.
"""

from __future__ import annotations

import dataclasses as dc
import json
import typing as typ

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_snapshot.producer.commands import (
    bundle_asset_name,
    bundle_command,
    launcher_asset_name,
    launcher_package_command,
    toolchain_install_command,
)
from rust_prover_tools.kani_snapshot.producer.lifecycle import (
    LIFECYCLE_SCHEMA_VERSION,
    WORKFLOW_PATH,
    WORKFLOW_REF,
    BuildPlan,
    BuildRequest,
    ResolutionRoute,
    parse_build_plan,
    parse_resolution,
    plan_build,
    plan_to_json,
    resolution_to_json,
    same_run,
)
from rust_prover_tools.kani_snapshot.producer.recipe import (
    pinned_recipe,
    recipe_digest,
)
from rust_prover_tools.unittests import kani_producer_fixtures as fixtures

if typ.TYPE_CHECKING:
    from syrupy.assertion import SnapshotAssertion

PINNED = pinned_recipe()
_RUN_IDS = st.integers(min_value=1, max_value=2**31 - 1)


def _plan(**overrides: object) -> BuildPlan:
    """Return the pinned build plan, applying overrides to the request."""
    values: dict[str, object] = {
        "resolution": fixtures.resolution(),
        "workflow": fixtures.workflow(),
        "builder_commit": fixtures.BUILDER_COMMIT,
        "tag": fixtures.TAG,
    }
    values.update(overrides)
    return plan_build(BuildRequest(**typ.cast("dict[str, typ.Any]", values)))


def test_a_resolution_round_trips_through_canonical_json() -> None:
    """The document a job writes is exactly the document the next job parses."""
    resolution = fixtures.resolution()
    assert parse_resolution(resolution_to_json(resolution)) == resolution


def test_a_build_plan_round_trips_through_canonical_json() -> None:
    """A plan's argument vectors survive canonical rendering unchanged."""
    plan = _plan()
    assert parse_build_plan(plan_to_json(plan)) == plan


def test_the_plan_records_the_pinned_recipe_argument_vectors() -> None:
    """Each command is an argument vector derived from the pinned recipe."""
    plan = _plan()
    assert plan.target == PINNED.target
    assert plan.recipe_digest == recipe_digest(PINNED)
    assert plan.bundle_asset == bundle_asset_name(
        PINNED.upstream.crate_version, PINNED.target
    )
    assert plan.launcher_asset == launcher_asset_name(
        PINNED.upstream.crate_version, PINNED.target
    )
    assert plan.commands == (
        toolchain_install_command(PINNED.upstream.toolchain_channel),
        bundle_command(PINNED.upstream.crate_version),
        launcher_package_command(PINNED.launcher.package),
    )


def test_the_rendered_plan_is_stable(snapshot: SnapshotAssertion) -> None:
    """Canonical plan bytes are part of the contract between producer jobs."""
    assert plan_to_json(_plan()).decode("ascii") == snapshot


def test_the_rendered_resolution_is_stable(snapshot: SnapshotAssertion) -> None:
    """Canonical resolution bytes are the contract between producer jobs."""
    assert resolution_to_json(fixtures.resolution()).decode("ascii") == snapshot


def test_the_manual_route_must_come_from_a_dispatch_run() -> None:
    """A manual resolution recorded under a schedule event is forged."""
    with pytest.raises(ProverToolError, match="workflow_dispatch"):
        fixtures.resolution(
            route=ResolutionRoute.MANUAL,
            identity=fixtures.workflow(event="schedule"),
        )


def test_the_scheduled_route_must_come_from_a_schedule_run() -> None:
    """A scheduled resolution recorded under a dispatch event is forged."""
    with pytest.raises(ProverToolError, match="schedule"):
        fixtures.resolution(
            route=ResolutionRoute.SCHEDULED,
            identity=fixtures.workflow(event="workflow_dispatch"),
        )


@pytest.mark.parametrize(
    ("path", "ref"),
    [
        (".github/workflows/kani-monthly-copy.yml", WORKFLOW_REF),
        (WORKFLOW_PATH, "refs/heads/topic"),
        (WORKFLOW_PATH, "refs/pull/7/merge"),
    ],
)
def test_a_resolution_from_another_workflow_or_ref_is_refused(
    path: str, ref: str
) -> None:
    """Only the producer workflow on the default branch may resolve."""
    with pytest.raises(ProverToolError):
        fixtures.resolution(
            identity=dc.replace(fixtures.workflow(), path=path, ref=ref)
        )


def test_a_resolution_refuses_a_short_upstream_commit() -> None:
    """An abbreviated commit cannot name an immutable upstream identity."""
    with pytest.raises(ProverToolError, match="40"):
        fixtures.resolution(upstream_commit=fixtures.UPSTREAM_COMMIT[:12])


def test_a_plan_refuses_a_resolution_from_another_run() -> None:
    """A resolution left by an earlier run must not authorise a build."""
    other = fixtures.workflow(run_id=999)
    with pytest.raises(ProverToolError, match="resolving run"):
        _plan(resolution=fixtures.resolution(identity=other))


def test_a_plan_refuses_a_resolution_from_another_attempt() -> None:
    """A rerun revalidates; it never inherits the previous attempt's plan."""
    resolution = fixtures.resolution(identity=fixtures.workflow(run_attempt=1))
    with pytest.raises(ProverToolError, match="resolving run"):
        _plan(resolution=resolution, workflow=fixtures.workflow(run_attempt=2))


def test_a_plan_refuses_an_upstream_commit_outside_the_recipe() -> None:
    """Building an unpinned upstream commit is not a producer operation."""
    resolution = dc.replace(fixtures.resolution(), upstream_commit="e" * 40)
    with pytest.raises(ProverToolError, match="pinned upstream commit"):
        _plan(resolution=resolution)


def test_a_plan_refuses_an_upstream_tag_outside_the_recipe() -> None:
    """The tag is evidence of the release, so it must be the pinned one."""
    resolution = dc.replace(fixtures.resolution(), upstream_tag="kani-0.69.0")
    with pytest.raises(ProverToolError, match="pinned upstream tag"):
        _plan(resolution=resolution)


def test_a_plan_refuses_a_tag_with_the_wrong_commit_prefix() -> None:
    """A tag naming another upstream commit cannot publish this build."""
    with pytest.raises(ProverToolError, match="prefix"):
        _plan(tag=f"kani-snapshot-2026-09-{'e' * 12}-r1")


@pytest.mark.parametrize(
    "commands",
    [((),), (("cargo", ""),)],
    ids=["empty-vector", "empty-argument"],
)
def test_a_plan_refuses_a_malformed_command(
    commands: tuple[tuple[str, ...], ...],
) -> None:
    """Every recorded command must be a non-empty argument vector."""
    with pytest.raises(ProverToolError, match="argument vectors"):
        BuildPlan(
            schema_version=LIFECYCLE_SCHEMA_VERSION,
            tag=fixtures.TAG,
            upstream_commit=fixtures.UPSTREAM_COMMIT,
            builder_commit=fixtures.BUILDER_COMMIT,
            target=PINNED.target,
            recipe_digest=recipe_digest(PINNED),
            bundle_asset="bundle.tar.gz",
            launcher_asset="launcher.tar.gz",
            commands=commands,
            workflow=fixtures.workflow(),
        )


def test_a_plan_without_commands_is_refused() -> None:
    """A plan that does nothing cannot be a build plan."""
    with pytest.raises(ProverToolError, match="at least one command"):
        dc.replace(_plan(), commands=())


@pytest.mark.parametrize("version", [0, 2, 99])
def test_an_unknown_schema_version_is_refused(version: int) -> None:
    """A future schema must fail closed rather than be guessed at."""
    with pytest.raises(ProverToolError, match="schema_version"):
        dc.replace(fixtures.resolution(), schema_version=version)


def test_parsing_refuses_an_unknown_key() -> None:
    """An unexpected field could otherwise be silently ignored."""
    document = json.loads(resolution_to_json(fixtures.resolution()))
    document["surprise"] = True
    with pytest.raises(ProverToolError, match="unknown"):
        parse_resolution(json.dumps(document).encode("ascii"))


def test_parsing_refuses_a_missing_key() -> None:
    """A partially written document must not be usable."""
    document = json.loads(resolution_to_json(fixtures.resolution()))
    del document["workflow"]
    with pytest.raises(ProverToolError, match="missing"):
        parse_resolution(json.dumps(document).encode("ascii"))


def test_parsing_refuses_an_unknown_route() -> None:
    """A route this producer does not implement is not a resolution."""
    document = json.loads(resolution_to_json(fixtures.resolution()))
    document["route"] = "guesswork"
    with pytest.raises(ProverToolError, match="manual or scheduled"):
        parse_resolution(json.dumps(document).encode("ascii"))


@given(run_id=_RUN_IDS, run_attempt=_RUN_IDS)
@settings(max_examples=25, deadline=None)
def test_same_run_requires_the_same_workflow_and_attempt(
    run_id: int, run_attempt: int
) -> None:
    """Run identity is the triple later jobs compare before any effect."""
    left = fixtures.workflow(run_id=run_id, run_attempt=run_attempt)
    assert same_run(left, fixtures.workflow(run_id=run_id, run_attempt=run_attempt))
    assert not same_run(
        left, fixtures.workflow(run_id=run_id + 1, run_attempt=run_attempt)
    )
    assert not same_run(
        left, fixtures.workflow(run_id=run_id, run_attempt=run_attempt + 1)
    )
    assert not same_run(
        left,
        dc.replace(
            fixtures.workflow(run_id=run_id, run_attempt=run_attempt), path="x.yml"
        ),
    )


@given(run_id=_RUN_IDS)
@settings(max_examples=25, deadline=None)
def test_a_resolution_round_trips_for_any_run_identity(run_id: int) -> None:
    """Canonical rendering never loses a run identity, however large."""
    resolution = fixtures.resolution(identity=fixtures.workflow(run_id=run_id))
    assert parse_resolution(resolution_to_json(resolution)) == resolution
