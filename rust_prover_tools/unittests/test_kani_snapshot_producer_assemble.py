"""Assembly turns payload files into the bytes a consumer pin hashes.

The manifest must record measured evidence, never a claim: sizes and digests
come from re-reading each payload, and `asset_bytes` is recomputed from those
measurements. These tests also hold the exactness rule — the payload directory
must contain precisely the pinned payload names — and the binding rule that the
input belongs to the run doing the assembly.
"""

from __future__ import annotations

import dataclasses as dc
import json
import typing as typ

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from rust_prover_tools.downloads import sha256_file
from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_snapshot.manifest import parse_manifest
from rust_prover_tools.kani_snapshot.models import ASSET_ROLES, SUPPORTED_TARGET
from rust_prover_tools.kani_snapshot.producer.assemble import (
    MANIFEST_NAME,
    AssemblyInput,
    AssemblyRequest,
    assemble,
    assembly_input_to_json,
    parse_assembly_input,
    payload_names,
)
from rust_prover_tools.unittests import kani_producer_fixtures as fixtures

if typ.TYPE_CHECKING:
    import pathlib

    from syrupy.assertion import SnapshotAssertion

    from rust_prover_tools.kani_snapshot.producer.assemble import Assembly
    from rust_prover_tools.kani_snapshot.producer.publication_identity import (
        PublicationIdentity,
    )


def _assemble(
    directory: pathlib.Path,
    inputs: AssemblyInput | None = None,
    identity: PublicationIdentity | None = None,
) -> Assembly:
    """Write the pinned payloads and assemble them for the trusted run."""
    fixtures.write_payloads(directory)
    return assemble(
        AssemblyRequest(
            payloads=directory,
            inputs=fixtures.assembly_input() if inputs is None else inputs,
            identity=(
                fixtures.publication_identity() if identity is None else identity
            ),
            workflow=fixtures.workflow(),
        )
    )


def test_assembly_measures_every_payload_and_the_resource_total(
    tmp_path: pathlib.Path,
) -> None:
    """The manifest's bytes are the files', not a claim carried in the input."""
    assembly = _assemble(tmp_path)
    names = fixtures.names()
    measured = {asset.role: asset for asset in assembly.manifest.assets}
    assert tuple(measured) == ASSET_ROLES
    for role in ASSET_ROLES:
        asset = measured[role]
        path = tmp_path / names[role]
        assert asset.name == names[role]
        assert asset.size == len(fixtures.PAYLOAD_BYTES[role])
        assert asset.sha256 == sha256_file(path)
    assert assembly.manifest.resources.asset_bytes == sum(
        asset.size for asset in assembly.manifest.assets
    )
    assert assembly.manifest.resources.wall_seconds == pytest.approx(1800.0)
    assert assembly.manifest.target == SUPPORTED_TARGET


def test_the_manifest_parses_back_to_the_same_identity(
    tmp_path: pathlib.Path,
) -> None:
    """The rendered manifest is exactly what the consumer parser accepts."""
    assembly = _assemble(tmp_path)
    parsed = parse_manifest(assembly.manifest_bytes)
    assert parsed.manifest == assembly.manifest
    assert parsed.sha256 == assembly.manifest_sha256


def test_checksums_cover_the_payloads_and_the_manifest(
    tmp_path: pathlib.Path,
) -> None:
    """`SHA256SUMS` names every payload plus `manifest.json`, sorted by name."""
    assembly = _assemble(tmp_path)
    rows = [
        line.split("  ", 1) for line in assembly.checksums.decode("ascii").splitlines()
    ]
    digests = {name: digest for digest, name in rows}
    assert set(digests) == {asset.name for asset in assembly.manifest.assets} | {
        MANIFEST_NAME
    }
    assert digests[MANIFEST_NAME] == assembly.manifest_sha256
    assert [name for _, name in rows] == sorted(name for _, name in rows)


def test_the_rendered_assembly_input_is_stable(snapshot: SnapshotAssertion) -> None:
    """The build job's document and the parser share one canonical shape."""
    assert assembly_input_to_json(fixtures.assembly_input()).decode("ascii") == snapshot


def test_the_assembly_input_round_trips_through_canonical_json() -> None:
    """Assembly re-reads the metadata a build job wrote without loss."""
    inputs = fixtures.assembly_input()
    assert parse_assembly_input(assembly_input_to_json(inputs)) == inputs


def test_assembly_refuses_an_input_from_another_run(tmp_path: pathlib.Path) -> None:
    """A build job's measurements cannot authorise another run's manifest."""
    with pytest.raises(ProverToolError, match="another workflow run"):
        _assemble(
            tmp_path,
            inputs=fixtures.assembly_input(identity=fixtures.workflow(run_id=999)),
        )


def test_assembly_refuses_an_input_from_another_attempt(
    tmp_path: pathlib.Path,
) -> None:
    """A rerun reassembles rather than inheriting the failed attempt."""
    with pytest.raises(ProverToolError, match="another workflow run"):
        _assemble(
            tmp_path,
            inputs=fixtures.assembly_input(identity=fixtures.workflow(run_attempt=2)),
        )


def test_assembly_refuses_an_upstream_commit_outside_the_recipe(
    tmp_path: pathlib.Path,
) -> None:
    """Payload names derive from the pinned recipe, so nothing else may pass."""
    other = "e" * 40
    identity = dc.replace(
        fixtures.publication_identity(),
        upstream_commit=other,
        tag=f"kani-snapshot-2026-09-{other[:12]}-r1",
    )
    with pytest.raises(ProverToolError, match="outside the pinned recipe"):
        _assemble(
            tmp_path,
            inputs=dc.replace(
                fixtures.assembly_input(),
                upstream_commit=other,
                tag=identity.tag,
            ),
            identity=identity,
        )


def test_assembly_refuses_a_tag_that_does_not_bind_the_commit(
    tmp_path: pathlib.Path,
) -> None:
    """The tag's 12-hex prefix must name the exact upstream commit."""
    with pytest.raises(ProverToolError, match="prefix"):
        _assemble(
            tmp_path,
            inputs=dc.replace(
                fixtures.assembly_input(), tag=f"kani-snapshot-2026-09-{'e' * 12}-r1"
            ),
        )


def test_assembly_refuses_a_tag_the_identity_did_not_allocate(
    tmp_path: pathlib.Path,
) -> None:
    """A build job cannot sign a different, otherwise valid tag into the manifest.

    Publication derives its target from the manifest, so a tag the resolve job
    never allocated would let the least trusted job choose what gets released.
    """
    other = fixtures.TAG.replace("-r1", "-r2")
    with pytest.raises(ProverToolError, match="not the allocated tag"):
        _assemble(
            tmp_path,
            identity=fixtures.publication_identity(tag=other),
        )


def test_assembly_refuses_a_builder_commit_the_identity_did_not_allocate(
    tmp_path: pathlib.Path,
) -> None:
    """A foreign builder commit cannot be signed as this run's provenance."""
    with pytest.raises(ProverToolError, match="not the allocated builder commit"):
        _assemble(
            tmp_path,
            identity=fixtures.publication_identity(builder_commit="f" * 40),
        )


def test_assembly_refuses_an_upstream_commit_the_identity_did_not_allocate(
    tmp_path: pathlib.Path,
) -> None:
    """The signed manifest must name the upstream commit the resolve job chose."""
    identity = dc.replace(fixtures.publication_identity(), upstream_commit="d" * 40)
    with pytest.raises(ProverToolError, match="not the allocated upstream commit"):
        _assemble(tmp_path, identity=identity)


def test_assembly_refuses_an_identity_from_another_run(tmp_path: pathlib.Path) -> None:
    """A stale identity cannot authorise this attempt's manifest."""
    with pytest.raises(ProverToolError, match="another workflow run"):
        _assemble(
            tmp_path,
            identity=fixtures.publication_identity(
                identity=fixtures.workflow(run_attempt=2)
            ),
        )


def test_assembly_refuses_a_missing_payload(tmp_path: pathlib.Path) -> None:
    """A partial payload set cannot describe a complete snapshot."""
    fixtures.write_payloads(tmp_path)
    (tmp_path / fixtures.names()["notices"]).unlink()
    with pytest.raises(ProverToolError, match="missing files"):
        assemble(
            AssemblyRequest(
                payloads=tmp_path,
                inputs=fixtures.assembly_input(),
                identity=fixtures.publication_identity(),
                workflow=fixtures.workflow(),
            )
        )


def test_assembly_refuses_an_unexpected_payload(tmp_path: pathlib.Path) -> None:
    """An extra file would otherwise be silently dropped from the release."""
    fixtures.write_payloads(tmp_path)
    (tmp_path / "surprise.tar.gz").write_bytes(b"unexpected")
    with pytest.raises(ProverToolError, match="unexpected files"):
        assemble(
            AssemblyRequest(
                payloads=tmp_path,
                inputs=fixtures.assembly_input(),
                identity=fixtures.publication_identity(),
                workflow=fixtures.workflow(),
            )
        )


def test_assembly_refuses_a_payload_that_is_not_a_file(
    tmp_path: pathlib.Path,
) -> None:
    """A directory or link in place of a payload is not a measured file."""
    fixtures.write_payloads(tmp_path)
    name = fixtures.names()["inventory"]
    (tmp_path / name).unlink()
    (tmp_path / name).mkdir()
    with pytest.raises(ProverToolError, match="regular file"):
        assemble(
            AssemblyRequest(
                payloads=tmp_path,
                inputs=fixtures.assembly_input(),
                identity=fixtures.publication_identity(),
                workflow=fixtures.workflow(),
            )
        )


def test_assembly_refuses_a_missing_payload_directory(
    tmp_path: pathlib.Path,
) -> None:
    """A path that names nothing cannot have been measured."""
    with pytest.raises(ProverToolError, match="does not exist"):
        assemble(
            AssemblyRequest(
                payloads=tmp_path / "absent",
                inputs=fixtures.assembly_input(),
                identity=fixtures.publication_identity(),
                workflow=fixtures.workflow(),
            )
        )


def test_payload_names_are_the_pinned_release_names() -> None:
    """Names come from one function, so a caller cannot rename an asset."""
    assert payload_names(fixtures.CRATE_VERSION, SUPPORTED_TARGET) == fixtures.names()


def test_payload_names_refuse_an_unsupported_target() -> None:
    """Only the one supported target has a payload layout."""
    with pytest.raises(ProverToolError):
        payload_names(fixtures.CRATE_VERSION, "aarch64-apple-darwin")


@pytest.mark.parametrize("wall_seconds", [-1.0, float("inf"), float("nan")])
def test_an_input_with_an_impossible_wall_time_is_refused(
    wall_seconds: float,
) -> None:
    """A measured duration must be finite and non-negative."""
    with pytest.raises(ProverToolError, match="wall_seconds"):
        fixtures.assembly_input(wall_seconds=wall_seconds)


def test_an_input_with_a_negative_disk_measurement_is_refused() -> None:
    """A high-water mark below zero cannot have been observed."""
    with pytest.raises(ProverToolError, match="disk_high_water_bytes"):
        dc.replace(fixtures.assembly_input(), disk_high_water_bytes=-1)


def test_parsing_refuses_an_unknown_input_key() -> None:
    """Unknown metadata could otherwise change meaning silently."""
    document = json.loads(assembly_input_to_json(fixtures.assembly_input()))
    document["surprise"] = True
    with pytest.raises(ProverToolError, match="unknown"):
        parse_assembly_input(json.dumps(document).encode("ascii"))


def test_parsing_refuses_a_missing_input_key() -> None:
    """A partially written document is not usable metadata."""
    document = json.loads(assembly_input_to_json(fixtures.assembly_input()))
    del document["resources"]
    with pytest.raises(ProverToolError, match="missing"):
        parse_assembly_input(json.dumps(document).encode("ascii"))


def test_parsing_refuses_a_boolean_wall_time() -> None:
    """`true` is not a measurement, however Python would coerce it."""
    document = json.loads(assembly_input_to_json(fixtures.assembly_input()))
    document["resources"]["wall_seconds"] = True
    with pytest.raises(ProverToolError, match="wall_seconds"):
        parse_assembly_input(json.dumps(document).encode("ascii"))


@given(
    payloads=st.fixed_dictionaries({
        role: st.binary(min_size=1, max_size=64) for role in ASSET_ROLES
    }),
    wall_seconds=st.floats(
        min_value=0, max_value=1e6, allow_nan=False, allow_infinity=False
    ),
)
@settings(max_examples=15, deadline=None)
def test_assembly_always_records_the_measured_bytes(
    tmp_path_factory: pytest.TempPathFactory,
    payloads: dict[str, bytes],
    wall_seconds: float,
) -> None:
    """Whatever the payload bytes are, the manifest records exactly those."""
    directory = tmp_path_factory.mktemp("payloads")
    names = fixtures.names()
    for role, name in names.items():
        (directory / name).write_bytes(payloads[role])
    assembly = assemble(
        AssemblyRequest(
            payloads=directory,
            inputs=fixtures.assembly_input(wall_seconds=wall_seconds),
            identity=fixtures.publication_identity(),
            workflow=fixtures.workflow(),
        )
    )
    measured = {asset.role: asset for asset in assembly.manifest.assets}
    for role in ASSET_ROLES:
        assert measured[role].size == len(payloads[role])
        assert measured[role].sha256 == sha256_file(directory / names[role])
    assert assembly.manifest.resources.asset_bytes == sum(
        len(data) for data in payloads.values()
    )
