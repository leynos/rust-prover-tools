"""Contract tests for the build job's run summary.

The summary is the only place a reader learns what a run measured, so it must
run on every outcome and read its measurements from the document the build
wrote rather than from the top level of an evidence file. These tests live
apart from the build-command contracts because they run the workflow's own
`jq` filter over real documents instead of checking the text of the commands
that produced them.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess

import pytest

from rust_prover_tools.kani_snapshot.producer.evidence_model import (
    evidence_to_json,
)
from rust_prover_tools.unittests import kani_producer_fixtures as producer
from tests.workflow_contracts.strict_workflow import (
    job,
    named_scripts,
    read_workflow,
    steps,
)

#: The single-quoted program the summary feeds to `jq -r`.
_SUMMARY_FILTER = re.compile(r"jq -r '([^']*)'")


@pytest.fixture(scope="module")
def workflow() -> dict[str, object]:
    """Return the parsed workflow shared by the whole module."""
    return read_workflow()


def _render(program: str, document: bytes) -> str:
    """Run a workflow jq filter over one document, as the summary does."""
    jq = shutil.which("jq")
    assert jq is not None, (
        "jq is required to check the run summary's filter; install it and see "
        "the host tools note in docs/developers-guide.md"
    )
    return subprocess.run(
        [jq, "-r", program],
        input=document,
        capture_output=True,
        check=True,
    ).stdout.decode("ascii")


def test_the_build_summarises_the_observations_in_the_run(
    workflow: dict[str, object],
) -> None:
    """D5's run summary runs on every outcome and reads its own files."""
    summaries = [
        step
        for step in steps(job(workflow, "build"))
        if step.get("name") == "Summarise the build in the run summary"
    ]
    assert len(summaries) == 1, "the build must declare exactly one run-summary step"
    assert summaries[0]["if"] == "always()", (
        "a summary that skipped on failure would hide the failing run's numbers"
    )
    script = named_scripts(job(workflow, "build"))[
        "Summarise the build in the run summary"
    ]
    for expected in ("$GITHUB_STEP_SUMMARY", "source-before", "source-after"):
        assert expected in script, expected


def test_the_summary_reads_the_measured_resources_out_of_the_evidence(
    workflow: dict[str, object],
) -> None:
    """The summary's jq filter must render the measured numbers, not `null`.

    The evidence document nests both measurements under `resources`, so an
    expression that reads them at the top level produces a plausible-looking
    row of `null`s after a build that actually succeeded. Running the
    workflow's own filter over a real evidence document is the only check that
    catches that: asserting the text of the script would accept either path.
    """
    script = named_scripts(job(workflow, "build"))[
        "Summarise the build in the run summary"
    ]
    programs = _SUMMARY_FILTER.findall(script)
    assert len(programs) == 1, "the summary should feed one filter to jq"

    measured = producer.evidence(
        wall_seconds=1234.5, disk_high_water_bytes=9_876_543_210
    )
    rendered = _render(programs[0], evidence_to_json(measured))

    assert "| Wall clock (s) | 1234.5 |" in rendered, (
        "the summary must render the fractional wall clock from resources"
    )
    assert "| Disk high-water (bytes) | 9876543210 |" in rendered, (
        "the summary must render the disk high-water mark from resources"
    )
    assert "| Cache outcome | miss |" in rendered, (
        "the summary must render the recorded cache outcome"
    )
    assert "null" not in rendered, "the summary must not render a null measurement"

    # A failed build has no evidence document, only the observations recorded
    # before the failure. The same filter must still render them.
    partial = _render(
        programs[0],
        json.dumps({
            "resources": {"wall_seconds": 42, "disk_high_water_bytes": 1024}
        }).encode("ascii"),
    )
    assert "| Wall clock (s) | 42 |" in partial, (
        "the summary must render the recorded integer wall clock"
    )
    assert "| Disk high-water (bytes) | 1024 |" in partial, (
        "the summary must render the recorded disk high-water mark"
    )
    assert "| Cache outcome | not measured |" in partial, (
        "a failed build's summary must mark the cache outcome as not measured"
    )
    assert "null" not in partial, (
        "a failed build's summary must not render a null measurement"
    )
