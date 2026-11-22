"""Contract tests for the guarded Kani snapshot workflow.

The snapshot workflow is declarative configuration around a tested producer,
so the risk is drift: a widened permission, a lost guard, an artefact fetched
by name from another run, an unpinned Action, or a budget that quietly grows.
Each test below pins one of those promises, reading the workflow with a
loader that refuses duplicate keys so a test cannot assert a shadowed value.

This module covers the guard, permissions, concurrency, budgets, artefact
routing and job order. The build job's own hand-offs — pinned solver digests,
bundle location, source-state evidence and the launcher archive — are pinned in
`tests/test_kani_workflow_build_contract.py`, which keeps both modules small
enough to read in one sitting.
"""

from __future__ import annotations

import re
import typing as typ

import pytest

from tests.workflow_contracts.strict_workflow import (
    artifact_names,
    as_mapping,
    as_sequence,
    dependency_names,
    job,
    named_scripts,
    read_workflow,
    run_scripts,
    steps,
    strict_load,
    triggers,
    uses_entries,
)

#: Job deadlines from D5, in minutes.
BUDGETS: typ.Final[dict[str, int]] = {
    "resolve": 10,
    "build": 180,
    "assemble": 15,
    "validate": 45,
    "publish": 15,
}

#: The exact permissions each job may hold. `None` means no `permissions` key.
PERMISSIONS: typ.Final[dict[str, dict[str, str] | None]] = {
    "guard": {},
    "resolve": {"contents": "read"},
    "build": {"actions": "read", "contents": "read"},
    "assemble": {
        "actions": "read",
        "attestations": "write",
        "contents": "read",
        "id-token": "write",
    },
    "validate": {"actions": "read", "contents": "read"},
    "publish": {"actions": "read", "contents": "write"},
}

_PIN = re.compile(r"^[^@\s]+@[0-9a-f]{40}$")


@pytest.fixture(scope="module")
def workflow() -> dict[str, object]:
    """Return the parsed workflow shared by the whole module."""
    return read_workflow()


def test_the_workflow_parses_strictly() -> None:
    """A duplicate key would make the tested value depend on YAML order."""
    with pytest.raises(Exception, match="duplicate key"):
        strict_load("jobs:\n  a: {}\n  a: {}\n")


def test_a_document_without_jobs_is_refused() -> None:
    """The contract reader refuses a document it cannot reason about."""
    with pytest.raises(TypeError, match="declares no"):
        job(strict_load("on: {}\n"), "build")


def test_only_schedule_and_dispatch_are_trusted_events(
    workflow: dict[str, object],
) -> None:
    """A pull request must never start the expensive, privileged workflow."""
    assert set(triggers(workflow)) == {"schedule", "workflow_dispatch"}


def test_the_schedule_is_monthly_and_opt_in(
    workflow: dict[str, object],
) -> None:
    """The cron is D5's, and a schedule stays disabled until it is enabled."""
    schedule = as_sequence(triggers(workflow)["schedule"], "schedule")
    assert [as_mapping(item, "cron")["cron"] for item in schedule] == ["23 4 3 * *"]

    guard = "\n".join(run_scripts(job(workflow, "guard")))
    assert "KANI_SNAPSHOTS_ENABLED" in guard
    assert '"$SNAPSHOTS_ENABLED" != "true"' in guard


def test_the_dispatch_inputs_are_the_documented_three(
    workflow: dict[str, object],
) -> None:
    """Operators get one commit, one tag and one publish switch."""
    dispatch = as_mapping(triggers(workflow)["workflow_dispatch"], "dispatch")
    inputs = as_mapping(dispatch["inputs"], "inputs")
    assert list(inputs) == ["upstream_commit", "publication_id", "publish"]
    assert as_mapping(inputs["upstream_commit"], "commit")["required"] is True
    assert as_mapping(inputs["publication_id"], "tag")["required"] is True
    publish = as_mapping(inputs["publish"], "publish")
    assert publish["type"] == "boolean"
    assert publish["default"] is False
    assert "attestation" in typ.cast("str", publish["description"])


def test_the_run_is_serialized_and_never_cancelled(
    workflow: dict[str, object],
) -> None:
    """Cancelling mid-publication would leave a draft nobody owns."""
    concurrency = as_mapping(workflow["concurrency"], "concurrency")
    assert typ.cast("str", concurrency["group"]).startswith("kani-snapshot-")
    assert concurrency["cancel-in-progress"] is False


def test_the_top_level_permissions_are_empty(
    workflow: dict[str, object],
) -> None:
    """Every capability is granted by the one job that needs it."""
    assert workflow["permissions"] == {}


@pytest.mark.parametrize("name", sorted(PERMISSIONS))
def test_each_job_holds_only_its_own_permissions(
    workflow: dict[str, object], name: str
) -> None:
    """A widened grant is a contract change, not a convenience."""
    assert job(workflow, name).get("permissions") == PERMISSIONS[name]


def test_only_publication_may_write_contents(
    workflow: dict[str, object],
) -> None:
    """D5 reserves the writer token for the one job that creates a release."""
    writers = [
        name
        for name in PERMISSIONS
        if as_mapping(job(workflow, name).get("permissions") or {}, "permissions").get(
            "contents"
        )
        == "write"
    ]
    assert writers == ["publish"]


def test_resolve_collects_the_releases_itself_and_stays_read_only(
    workflow: dict[str, object],
) -> None:
    """The allocator reads published identities with the read-only token.

    GitHub lists draft releases only to a push-capable token, so numbering
    here sees published releases alone. A draft left by an interrupted run is
    invisible to this job and is refused by publication, which holds the
    writer token and re-observes before it writes.
    """
    declaration = job(workflow, "resolve")
    assert declaration["permissions"] == {"contents": "read"}

    collection = named_scripts(declaration)["Collect the existing releases"]
    assert "collect_releases" in collection
    assert "--output producer/releases.json" in collection
    collection_step = next(
        step
        for step in steps(declaration)
        if step.get("name") == "Collect the existing releases"
    )
    assert collection_step["if"] == "github.event_name == 'schedule'"

    identity = named_scripts(declaration)["Allocate the publication identity"]
    assert "--releases producer/releases.json" in identity


def test_only_the_attestation_job_may_sign(
    workflow: dict[str, object],
) -> None:
    """OIDC and attestation writes belong to one job with no build input run."""
    signing = [
        name
        for name in PERMISSIONS
        if as_mapping(job(workflow, name).get("permissions") or {}, "permissions").get(
            "id-token"
        )
    ]
    assert signing == ["assemble"]
    assemble = as_mapping(job(workflow, "assemble")["permissions"], "permissions")
    assert assemble["attestations"] == "write"


@pytest.mark.parametrize("name", sorted(BUDGETS))
def test_each_job_budget_matches_the_design(
    workflow: dict[str, object], name: str
) -> None:
    """Exceeding a budget is a review, not an automatic upgrade."""
    assert job(workflow, name)["timeout-minutes"] == BUDGETS[name]


def test_every_action_is_pinned_to_a_full_commit(
    workflow: dict[str, object],
) -> None:
    """A tag can be moved after review; a commit cannot."""
    references = uses_entries(workflow)
    assert references
    for reference in references:
        assert _PIN.match(reference), reference


def test_every_checkout_keeps_credentials_out_of_the_tree(
    workflow: dict[str, object],
) -> None:
    """Builder and upstream checkouts are data, never credential stores."""
    checkouts = [
        step
        for name in typ.cast("dict[str, object]", workflow["jobs"])
        for step in steps(job(workflow, name))
        if typ.cast("str", step.get("uses", "")).startswith("actions/checkout@")
    ]
    assert checkouts
    for checkout in checkouts:
        with_ = as_mapping(checkout["with"], "checkout inputs")
        assert with_["persist-credentials"] is False
        assert with_.get("ref") in {
            "${{ github.sha }}",
            "${{ needs.resolve.outputs.upstream_commit }}",
        }


def test_artefacts_are_only_exchanged_within_the_same_run(
    workflow: dict[str, object],
) -> None:
    """A name from another run is not evidence about this one."""
    uploaded, downloaded = artifact_names(workflow)
    assert uploaded
    assert set(downloaded) <= set(uploaded)
    for name in uploaded + downloaded:
        assert "${{ github.run_id }}" in name
        assert "${{ github.run_attempt }}" in name


def test_attestation_precedes_clean_validation(
    workflow: dict[str, object],
) -> None:
    """The consumer verifier must see the exact manifest before publication."""
    assert dependency_names(job(workflow, "validate")) == ["guard", "assemble"]
    assert dependency_names(job(workflow, "publish")) == ["guard", "validate"]

    assemble = [
        step
        for step in steps(job(workflow, "assemble"))
        if "attest-build-provenance@" in typ.cast("str", step.get("uses", ""))
    ]
    assert len(assemble) == 1
    with_ = as_mapping(assemble[0]["with"], "attestation inputs")
    assert with_["subject-path"] == "release/manifest.json"

    recorded = "\n".join(run_scripts(job(workflow, "assemble")))
    assert "release/manifest.sigstore.jsonl" in recorded


def test_assembly_binds_the_build_metadata_to_the_resolve_job_s_identity(
    workflow: dict[str, object],
) -> None:
    """Assembly must read the identity from the job the build cannot rewrite.

    The build artefact is produced by the least trusted job, so a tag or
    commit taken from it would let that job choose the publication target.
    The identity is therefore downloaded from the resolve job separately.
    """
    downloads = [
        as_mapping(step["with"], "download inputs")
        for step in steps(job(workflow, "assemble"))
        if "kani-snapshot-resolution-" in str(step.get("with", ""))
    ]
    assert len(downloads) == 1
    assert (
        "kani-snapshot-resolution-${{ github.run_id }}-${{ github.run_attempt }}"
        in str(downloads[0]["name"])
    )
    assert downloads[0]["path"] == "resolution", (
        "the identity must land outside the build artefact's own tree"
    )

    assemble = "\n".join(run_scripts(job(workflow, "assemble")))
    assert "--identity resolution/identity.json" in assemble


def test_publication_needs_this_run_s_validation_receipt(
    workflow: dict[str, object],
) -> None:
    """A receipt from another attempt cannot authorise this publication."""
    publish = "\n".join(run_scripts(job(workflow, "publish")))
    assert "--receipt validation/receipt.json" in publish
    downloaded = [
        step
        for step in steps(job(workflow, "publish"))
        if "kani-snapshot-validation-" in str(step.get("with", ""))
    ]
    assert len(downloaded) == 1


def test_the_privileged_jobs_never_run_upstream_code(
    workflow: dict[str, object],
) -> None:
    """Assembly, validation and publication see bytes, not a build tree."""
    for name in ("assemble", "validate", "publish"):
        declared = " ".join(run_scripts(job(workflow, name)))
        assert "upstream/" not in declared, name
        assert "cargo " not in declared, name
