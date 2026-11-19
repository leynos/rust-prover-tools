"""Contract tests for the ordinary Python release workflow.

This workflow publishes the consumer wheel for a `v*.*.*` tag, and it used to
create a public release before uploading anything. Published assets are
immutable, so a failed upload left a release nobody could complete. The
regressions below pin the corrected lifecycle: build a non-empty inventory,
create or resume a draft, upload only what is missing, prove the release
serves exactly the expected bytes, and publish last.

The wheel itself must not change: the same composite action, interpreter and
artefact name keep the release's contents stable across this refactor.
"""

from __future__ import annotations

import pathlib
import re
import typing as typ

import pytest

from tests.workflow_contracts.strict_workflow import (
    REPO_ROOT,
    as_mapping,
    dependency_names,
    job,
    named_scripts,
    read_workflow,
    steps,
    strict_load,
    triggers,
    uses_entries,
)

RELEASE_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "release.yml"
WHEEL_ACTION = REPO_ROOT / ".github" / "actions" / "pure-python-wheel" / "action.yml"

_PIN = re.compile(r"^[^@\s]+@[0-9a-f]{40}$")

#: The names of the release job's steps, in the order they must run.
EXPECTED_ORDER: typ.Final[list[str]] = [
    "Download the wheels",
    "Require a non-empty wheel inventory",
    "Create the draft or resume it",
    "Upload the missing wheels",
    "Verify the complete asset set",
    "Publish the release",
]


@pytest.fixture(scope="module")
def workflow() -> dict[str, object]:
    """Return the parsed release workflow."""
    return read_workflow(RELEASE_WORKFLOW)


def test_the_release_still_triggers_on_version_tags(
    workflow: dict[str, object],
) -> None:
    """The `v*.*.*` namespace is the release contract with consumers."""
    assert set(triggers(workflow)) == {"push"}
    push = as_mapping(triggers(workflow)["push"], "push")
    assert push["tags"] == ["v*.*.*"]
    assert "branches" not in push


def test_an_in_flight_release_is_not_cancelled(
    workflow: dict[str, object],
) -> None:
    """Cancelling could strand a draft with a partial asset set."""
    concurrency = as_mapping(workflow["concurrency"], "concurrency")
    assert concurrency["cancel-in-progress"] is False


def test_the_release_job_is_the_only_writer(
    workflow: dict[str, object],
) -> None:
    """Building a wheel needs no write access at all."""
    assert workflow["permissions"] == {}
    assert job(workflow, "pure-wheel")["permissions"] == {"contents": "read"}
    permissions = as_mapping(job(workflow, "release")["permissions"], "permissions")
    assert permissions == {"actions": "read", "contents": "write"}


def test_every_action_is_pinned_to_a_full_commit(
    workflow: dict[str, object],
) -> None:
    """A moved tag would change what runs at release time."""
    references = [
        reference
        for reference in uses_entries(workflow)
        if not reference.startswith("./")
    ]
    assert references
    for reference in references:
        assert _PIN.match(reference), reference


def test_the_release_steps_run_in_the_safe_order(
    workflow: dict[str, object],
) -> None:
    """Verification cannot be skipped by reordering the job."""
    names = [
        typ.cast("str", step["name"])
        for step in steps(job(workflow, "release"))
        if "name" in step
    ]
    assert [name for name in names if name in EXPECTED_ORDER] == EXPECTED_ORDER


def test_an_empty_wheel_inventory_refuses_to_release(
    workflow: dict[str, object],
) -> None:
    """A release with no asset is worse than no release."""
    inventory = named_scripts(job(workflow, "release"))[
        "Require a non-empty wheel inventory"
    ]
    assert "*.whl" in inventory
    assert "${#wheels[@]} -eq 0" in inventory
    assert "refusing to create a release" in inventory


def test_a_published_release_is_never_touched(
    workflow: dict[str, object],
) -> None:
    """Published bytes are immutable, so a rerun must refuse."""
    draft = named_scripts(job(workflow, "release"))["Create the draft or resume it"]
    assert "isDraft" in draft
    assert '!= "true"' in draft
    assert "already published" in draft
    assert "resuming the existing draft" in draft


def test_the_release_is_created_as_a_draft_with_generated_notes(
    workflow: dict[str, object],
) -> None:
    """The public release appears only after every asset is verified."""
    draft = named_scripts(job(workflow, "release"))["Create the draft or resume it"]
    assert "gh release create" in draft
    assert "--draft" in draft
    assert "--generate-notes" in draft
    assert "--verify-tag" in draft


def test_only_missing_wheels_are_uploaded(
    workflow: dict[str, object],
) -> None:
    """A resumed draft must not be asked to replace an existing asset."""
    upload = named_scripts(job(workflow, "release"))["Upload the missing wheels"]
    assert "already uploaded" in upload
    assert "gh release upload" in upload
    assert "--clobber" not in upload


def test_the_release_is_verified_against_downloaded_bytes(
    workflow: dict[str, object],
) -> None:
    """A successful upload is not proof of the bytes a consumer receives.

    The release serves assets by basename, so the byte comparison must map a
    local build path to the name GitHub actually serves. Comparing the local
    path against the download directory would fail for every wheel, or worse,
    compare two different paths that happen to share a name.
    """
    verify = named_scripts(job(workflow, "release"))["Verify the complete asset set"]
    assert "gh release download" in verify
    assert 'cmp "$wheel" "verified/$(basename "$wheel")"' in verify
    assert "wheel-inventory.txt" in verify
    assert "diff -u wheel-names.txt downloaded.txt" in verify
    assert "diff -u wheel-names.txt remote.txt" in verify
    assert "outside the expected inventory" in verify


def test_publication_is_the_last_effect(
    workflow: dict[str, object],
) -> None:
    """No publish command exists outside the final step."""
    named = named_scripts(job(workflow, "release"))
    publishing = [name for name, script in named.items() if "--draft=false" in script]
    assert publishing == ["Publish the release"]
    assert list(named)[-1] == "Publish the release"


def test_the_wheel_contents_stay_stable() -> None:
    """The consumer artifact is the same wheel this refactor started with."""
    action = strict_load(WHEEL_ACTION.read_text(encoding="utf-8"))
    runs = as_mapping(action["runs"], "runs")
    body = "\n".join(
        typ.cast("str", step["run"])
        for step in typ.cast("list[dict[str, object]]", runs["steps"])
        if "run" in step
    )
    assert "uv build --wheel" in body

    release = read_workflow(RELEASE_WORKFLOW)
    wheel_job = job(release, "pure-wheel")
    local = [
        step
        for step in steps(wheel_job)
        if typ.cast("str", step.get("uses", "")).startswith("./")
    ]
    assert [step["uses"] for step in local] == ["./.github/actions/pure-python-wheel"]
    assert as_mapping(local[0]["with"], "wheel inputs") == {
        "python-version": "3.13",
        "artifact-name": "wheels-pure",
    }
    assert dependency_names(job(release, "release")) == ["pure-wheel"]


def test_the_release_downloads_only_its_own_artifact(
    workflow: dict[str, object],
) -> None:
    """The wheel set comes from this run's build, not from a stale artefact."""
    download = [
        step
        for step in steps(job(workflow, "release"))
        if typ.cast("str", step.get("uses", "")).startswith(
            "actions/download-artifact@"
        )
    ]
    assert len(download) == 1
    assert as_mapping(download[0]["with"], "download inputs")["name"] == "wheels-pure"


def test_the_workflow_is_read_from_the_repository() -> None:
    """A moved workflow file would otherwise make every test vacuous."""
    assert pathlib.Path(RELEASE_WORKFLOW).is_file()
    assert pathlib.Path(WHEEL_ACTION).is_file()
