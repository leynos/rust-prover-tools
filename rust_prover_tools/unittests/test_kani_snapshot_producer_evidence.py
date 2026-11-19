"""Build evidence is measured once, then read back under the same schema.

The evidence document is the seam between the build job and assembly. These
tests hold the two properties that seam needs: the document the build job
writes is exactly the document assembly reads, and a plan from one run cannot
be combined with measurements from another. A refusal is tested for each
identity the manifest would otherwise record wrongly.
"""

from __future__ import annotations

import dataclasses as dc
import json
import typing as typ

import pytest

from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_snapshot.producer.assemble import (
    AssemblyRequest,
    assemble,
)
from rust_prover_tools.kani_snapshot.producer.build_evidence import (
    assembly_input_from_evidence,
    evidence_to_json,
    main,
    parse_build_evidence,
)
from rust_prover_tools.kani_snapshot.producer.lifecycle import plan_to_json
from rust_prover_tools.unittests import kani_producer_fixtures as fixtures

if typ.TYPE_CHECKING:
    import pathlib


def _document(**overrides: object) -> bytes:
    """Return the fixture evidence document with top-level overrides applied."""
    document = json.loads(evidence_to_json(fixtures.evidence()))
    assert isinstance(document, dict)
    document.update(overrides)
    return json.dumps(document).encode()


def test_evidence_round_trips_through_its_canonical_document() -> None:
    """The build job's document is exactly what assembly reads back."""
    measured = fixtures.evidence()
    assert parse_build_evidence(evidence_to_json(measured)) == measured


def test_unknown_evidence_keys_are_refused() -> None:
    """A field this implementation does not understand cannot be ignored."""
    with pytest.raises(ProverToolError, match="unknown keys"):
        parse_build_evidence(_document(unexpected="value"))


def test_missing_evidence_keys_are_refused() -> None:
    """A document missing a measurement is not a partial success."""
    document = json.loads(_document())
    del document["resources"]
    with pytest.raises(ProverToolError, match="missing keys"):
        parse_build_evidence(json.dumps(document).encode())


def test_another_evidence_schema_is_refused() -> None:
    """A schema this producer does not run fails closed."""
    with pytest.raises(ProverToolError, match="schema_version must be 2"):
        parse_build_evidence(_document(schema_version=1))


def test_the_derived_input_carries_recipe_owned_fields() -> None:
    """Assembly receives the recipe's runtime, prerequisites and digest."""
    measured = fixtures.evidence()
    assembly_input = assembly_input_from_evidence(
        plan=fixtures.build_plan(),
        workflow=fixtures.workflow(),
        evidence=measured,
    )

    assert assembly_input.tag == fixtures.TAG
    assert assembly_input.upstream_commit == fixtures.UPSTREAM_COMMIT
    assert assembly_input.builder_commit == fixtures.BUILDER_COMMIT
    assert assembly_input.runtime == fixtures.PINNED.runtime
    assert assembly_input.prerequisites == fixtures.PINNED.prerequisites
    assert assembly_input.build.submodules == fixtures.PINNED.submodules
    assert assembly_input.build.recipe_digest == fixtures.build_plan().recipe_digest
    assert assembly_input.wall_seconds == measured.wall_seconds


def test_the_derived_input_assembles_the_exact_payloads(tmp_path: pathlib.Path) -> None:
    """A plan plus evidence produces the manifest the same payloads deserve."""
    fixtures.write_payloads(tmp_path)
    assembly_input = assembly_input_from_evidence(
        plan=fixtures.build_plan(),
        workflow=fixtures.workflow(),
        evidence=fixtures.evidence(),
    )

    assembly = assemble(
        AssemblyRequest(
            payloads=tmp_path,
            inputs=assembly_input,
            identity=fixtures.publication_identity(),
            workflow=fixtures.workflow(),
        )
    )

    assert assembly.manifest.tag == fixtures.TAG
    assert assembly.manifest.workflow == fixtures.workflow()


def test_a_plan_from_another_attempt_is_refused() -> None:
    """Measurements from one attempt cannot assemble another attempt's plan."""
    other = fixtures.workflow(run_attempt=2)
    with pytest.raises(ProverToolError, match="another workflow run"):
        assembly_input_from_evidence(
            plan=fixtures.build_plan(),
            workflow=other,
            evidence=fixtures.evidence(),
        )


def test_a_backend_revision_other_than_the_resolved_commit_is_refused() -> None:
    """The backend must be the one built from the resolved upstream commit."""
    measured = fixtures.evidence()
    foreign = dc.replace(
        measured,
        backend=dc.replace(measured.backend, revision="f" * 40),
    )

    with pytest.raises(ProverToolError, match="backend revision"):
        assembly_input_from_evidence(
            plan=fixtures.build_plan(),
            workflow=fixtures.workflow(),
            evidence=foreign,
        )


def test_a_launcher_version_other_than_the_pinned_one_is_refused() -> None:
    """A launcher from another release cannot enter the pinned manifest."""
    foreign = dc.replace(fixtures.evidence(), launcher_package_version="0.67.0")

    with pytest.raises(ProverToolError, match="launcher version"):
        assembly_input_from_evidence(
            plan=fixtures.build_plan(),
            workflow=fixtures.workflow(),
            evidence=foreign,
        )


def test_the_command_writes_the_assembly_input(tmp_path: pathlib.Path) -> None:
    """The build job's command writes a document assembly accepts."""
    plan = tmp_path / "plan.json"
    measurements = tmp_path / "evidence.json"
    output = tmp_path / "assembly-input.json"
    plan.write_bytes(plan_to_json(fixtures.build_plan()))
    measurements.write_bytes(evidence_to_json(fixtures.evidence()))

    status = main([
        "--run-id",
        str(fixtures.workflow().run_id),
        "--run-attempt",
        str(fixtures.workflow().run_attempt),
        "--event",
        fixtures.workflow().event,
        "--plan",
        str(plan),
        "--evidence",
        str(measurements),
        "--output",
        str(output),
    ])

    assert status == 0
    assert output.read_bytes().startswith(b"{\n")


def test_the_command_reports_a_missing_document(tmp_path: pathlib.Path) -> None:
    """A missing input is a reported failure, not an unhandled traceback."""
    status = main([
        "--run-id",
        "1",
        "--run-attempt",
        "1",
        "--event",
        "schedule",
        "--plan",
        str(tmp_path / "absent.json"),
        "--evidence",
        str(tmp_path / "absent-evidence.json"),
        "--output",
        str(tmp_path / "out.json"),
    ])

    assert status == 1


def test_evidence_records_a_lockfile_path_once() -> None:
    """A repeated lockfile would make input equality ambiguous."""
    with pytest.raises(ProverToolError, match="more than once"):
        dc.replace(
            fixtures.evidence(),
            lock_digests=(("Cargo.lock", "8" * 64), ("Cargo.lock", "9" * 64)),
        )


def test_a_build_evidence_record_requires_the_supported_schema() -> None:
    """The record itself refuses a schema it cannot represent."""
    with pytest.raises(ProverToolError, match="schema_version must be 2"):
        dc.replace(fixtures.evidence(), schema_version=1)


def test_duplicate_lockfile_entries_are_refused_on_parse() -> None:
    """The document form refuses the same ambiguity as the record."""
    document = json.loads(_document())
    document["lock_digests"] = [
        {"path": "Cargo.lock", "sha256": "8" * 64},
        {"path": "Cargo.lock", "sha256": "9" * 64},
    ]
    with pytest.raises(ProverToolError, match="more than once"):
        parse_build_evidence(json.dumps(document).encode())
