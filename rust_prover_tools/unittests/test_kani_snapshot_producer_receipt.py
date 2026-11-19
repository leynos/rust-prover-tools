"""A validation receipt releases publication, so it must be narrow evidence.

The receipt says that clean validation passed for one exact manifest, in one
run and attempt. It is refused when it names another run, another tag, another
manifest digest or an incomplete asset set, and it is only ever accepted after
the publisher has re-hashed the payloads it names. These tests hold each of
those refusals individually, because a receipt that is merely "present" is the
easiest thing in the pipeline to forge by accident.
"""

from __future__ import annotations

import dataclasses as dc
import json
import typing as typ

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_snapshot.orchestrator.release_evidence import (
    release_asset_names,
    verify_payloads,
)
from rust_prover_tools.kani_snapshot.producer.validation_receipt import (
    RECEIPT_SCHEMA_VERSION,
    ValidationReceipt,
    build_receipt,
    parse_receipt,
    receipt_to_json,
    require_same_run,
    verify_receipt,
)
from rust_prover_tools.kani_snapshot.release_policy import ValidationBinding
from rust_prover_tools.unittests import kani_producer_fixtures as fixtures

if typ.TYPE_CHECKING:
    import pathlib

    from syrupy.assertion import SnapshotAssertion

_RUN_IDS = st.integers(min_value=1, max_value=2**31 - 1)
_CHECKS: typ.Final[tuple[str, ...]] = ("manifest-digest", "payload-digest")


def _receipt(tmp_path: pathlib.Path) -> ValidationReceipt:
    """Return the receipt the validate job would write for the pinned build."""
    return build_receipt(
        fixtures.workflow(),
        fixtures.parsed_manifest(tmp_path),
        _CHECKS,
        verification_passed=True,
    )


def test_a_receipt_names_the_validated_manifest_and_run(
    tmp_path: pathlib.Path,
) -> None:
    """Every field is evidence read from the parsed manifest, not a claim."""
    parsed = fixtures.parsed_manifest(tmp_path)
    receipt = build_receipt(
        fixtures.workflow(), parsed, _CHECKS, verification_passed=True
    )
    assert receipt.schema_version == RECEIPT_SCHEMA_VERSION
    assert receipt.run_id == fixtures.workflow().run_id
    assert receipt.run_attempt == fixtures.workflow().run_attempt
    assert receipt.tag == parsed.manifest.tag
    assert receipt.manifest_sha256 == parsed.sha256
    assert receipt.assets == release_asset_names(parsed.manifest)
    assert receipt.checks == _CHECKS
    assert receipt.verification_passed


def test_a_receipt_binds_publication_to_the_same_evidence(
    tmp_path: pathlib.Path,
) -> None:
    """The release policy's binding is the receipt's own fields."""
    receipt = _receipt(tmp_path)
    assert receipt.binding() == ValidationBinding(
        run_id=receipt.run_id,
        run_attempt=receipt.run_attempt,
        manifest_sha256=receipt.manifest_sha256,
        assets=receipt.assets,
        verification_passed=True,
    )


def test_a_receipt_round_trips_through_canonical_json(
    tmp_path: pathlib.Path,
) -> None:
    """The publish job re-reads exactly the document the validate job wrote."""
    receipt = _receipt(tmp_path)
    assert parse_receipt(receipt_to_json(receipt)) == receipt


def test_the_rendered_receipt_is_stable(
    tmp_path: pathlib.Path, snapshot: SnapshotAssertion
) -> None:
    """The receipt's bytes are a document other jobs compare, not a report."""
    assert receipt_to_json(_receipt(tmp_path)).decode("ascii") == snapshot


def test_a_receipt_from_another_run_is_refused(tmp_path: pathlib.Path) -> None:
    """A receipt cannot cross runs, however complete it looks."""
    with pytest.raises(ProverToolError, match="another workflow run or attempt"):
        require_same_run(_receipt(tmp_path), fixtures.workflow(run_id=999))


def test_a_receipt_from_another_attempt_is_refused(tmp_path: pathlib.Path) -> None:
    """A rerun revalidates; it never inherits the failed attempt's verdict."""
    with pytest.raises(ProverToolError, match="another workflow run or attempt"):
        require_same_run(_receipt(tmp_path), fixtures.workflow(run_attempt=2))


def test_a_receipt_recording_failure_is_refused(tmp_path: pathlib.Path) -> None:
    """A failed validation is evidence of the opposite of what is needed."""
    parsed = fixtures.parsed_manifest(tmp_path)
    receipt = build_receipt(
        fixtures.workflow(), parsed, _CHECKS, verification_passed=False
    )
    with pytest.raises(ProverToolError, match="failed validation"):
        verify_receipt(receipt, parsed)


def test_a_receipt_for_another_tag_is_refused(tmp_path: pathlib.Path) -> None:
    """The receipt must name the exact tag being published."""
    parsed = fixtures.parsed_manifest(tmp_path)
    receipt = dc.replace(_receipt(tmp_path), tag=fixtures.TAG.replace("-r1", "-r2"))
    with pytest.raises(ProverToolError, match="another tag"):
        verify_receipt(receipt, parsed)


def test_a_receipt_for_another_manifest_digest_is_refused(
    tmp_path: pathlib.Path,
) -> None:
    """The receipt binds bytes, not just metadata."""
    parsed = fixtures.parsed_manifest(tmp_path)
    receipt = dc.replace(_receipt(tmp_path), manifest_sha256="f" * 64)
    with pytest.raises(ProverToolError, match="another manifest digest"):
        verify_receipt(receipt, parsed)


def test_a_receipt_with_an_incomplete_asset_set_is_refused(
    tmp_path: pathlib.Path,
) -> None:
    """A receipt that skips an asset never covered its bytes."""
    parsed = fixtures.parsed_manifest(tmp_path)
    receipt = _receipt(tmp_path)
    receipt = dc.replace(receipt, assets=receipt.assets[:-1])
    with pytest.raises(ProverToolError, match="incomplete asset set"):
        verify_receipt(receipt, parsed)


def test_payloads_matching_the_manifest_are_accepted(tmp_path: pathlib.Path) -> None:
    """Verified payloads are the only thing the publisher will upload."""
    verify_payloads(fixtures.parsed_manifest(tmp_path), tmp_path)


def test_a_missing_payload_is_refused(tmp_path: pathlib.Path) -> None:
    """A payload that is not there cannot have been validated."""
    parsed = fixtures.parsed_manifest(tmp_path)
    (tmp_path / fixtures.names()["inventory"]).unlink()
    with pytest.raises(ProverToolError, match="not a regular file"):
        verify_payloads(parsed, tmp_path)


def test_a_payload_of_the_wrong_size_is_refused(tmp_path: pathlib.Path) -> None:
    """A same-named file with other bytes is not the validated payload."""
    parsed = fixtures.parsed_manifest(tmp_path)
    path = tmp_path / fixtures.names()["backend"]
    path.write_bytes(b"short")
    with pytest.raises(ProverToolError, match="size does not match"):
        verify_payloads(parsed, tmp_path)


def test_a_payload_with_the_wrong_digest_is_refused(tmp_path: pathlib.Path) -> None:
    """A payload of the right length but other bytes is still not validated."""
    parsed = fixtures.parsed_manifest(tmp_path)
    name = "backend"
    path = tmp_path / fixtures.names()[name]
    original = path.read_bytes()
    path.write_bytes(bytes(len(original)))
    with pytest.raises(ProverToolError, match="digest does not match"):
        verify_payloads(parsed, tmp_path)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"schema_version": 2}, "schema_version"),
        ({"run_id": 0}, "run_id"),
        ({"run_attempt": 0}, "run_attempt"),
        ({"tag": "kani-snapshot-x"}, "immutable snapshot grammar"),
        ({"manifest_sha256": "0" * 63}, "sha256"),
        ({"assets": ()}, "at least one asset"),
        ({"checks": ()}, "at least one check"),
    ],
)
def test_an_incomplete_receipt_cannot_be_constructed(
    overrides: dict[str, object], message: str
) -> None:
    """A receipt that cannot release publication is refused at construction."""
    values: dict[str, object] = {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "run_id": 1,
        "run_attempt": 1,
        "tag": fixtures.TAG,
        "manifest_sha256": "c" * 64,
        "assets": ("backend.tar.gz",),
        "verification_passed": True,
        "checks": ("manifest-digest",),
    }
    values.update(overrides)
    with pytest.raises(ProverToolError, match=message):
        ValidationReceipt(**typ.cast("dict[str, typ.Any]", values))


def test_a_receipt_repeating_an_asset_is_refused() -> None:
    """A repeated name would make the count of covered assets ambiguous."""
    with pytest.raises(ProverToolError, match="repeats an asset"):
        ValidationReceipt(
            schema_version=RECEIPT_SCHEMA_VERSION,
            run_id=1,
            run_attempt=1,
            tag=fixtures.TAG,
            manifest_sha256="c" * 64,
            assets=("backend.tar.gz", "backend.tar.gz"),
            verification_passed=True,
            checks=("manifest-digest",),
        )


def test_a_receipt_repeating_a_check_is_refused() -> None:
    """A repeated label would inflate the evidence the verdict rests on."""
    with pytest.raises(ProverToolError, match="must not repeat"):
        ValidationReceipt(
            schema_version=RECEIPT_SCHEMA_VERSION,
            run_id=1,
            run_attempt=1,
            tag=fixtures.TAG,
            manifest_sha256="c" * 64,
            assets=("backend.tar.gz",),
            verification_passed=True,
            checks=("manifest-digest", "manifest-digest"),
        )


def test_a_receipt_check_must_be_non_blank() -> None:
    """A blank label cannot name a check the validation performed."""
    with pytest.raises(ProverToolError, match="validation check"):
        ValidationReceipt(
            schema_version=RECEIPT_SCHEMA_VERSION,
            run_id=1,
            run_attempt=1,
            tag=fixtures.TAG,
            manifest_sha256="c" * 64,
            assets=("backend.tar.gz",),
            verification_passed=True,
            checks=("",),
        )


@pytest.mark.parametrize("asset", ["../escape.tar.gz", "nested/payload.tar.gz", "."])
def test_a_receipt_asset_must_be_a_plain_basename(asset: str) -> None:
    """A path in an asset name could escape the payload directory."""
    with pytest.raises(ProverToolError, match="plain basenames"):
        ValidationReceipt(
            schema_version=RECEIPT_SCHEMA_VERSION,
            run_id=1,
            run_attempt=1,
            tag=fixtures.TAG,
            manifest_sha256="c" * 64,
            assets=(asset,),
            verification_passed=True,
            checks=("manifest-digest",),
        )


def test_parsing_refuses_an_unknown_key(tmp_path: pathlib.Path) -> None:
    """An unexpected field could otherwise change the verdict silently."""
    document = json.loads(receipt_to_json(_receipt(tmp_path)))
    document["surprise"] = True
    with pytest.raises(ProverToolError, match="unknown"):
        parse_receipt(json.dumps(document).encode("ascii"))


def test_parsing_refuses_a_missing_key(tmp_path: pathlib.Path) -> None:
    """A partially written receipt is not evidence."""
    document = json.loads(receipt_to_json(_receipt(tmp_path)))
    del document["verification_passed"]
    with pytest.raises(ProverToolError, match="missing"):
        parse_receipt(json.dumps(document).encode("ascii"))


@pytest.mark.parametrize("value", [1, "true", None])
def test_parsing_refuses_a_non_boolean_verdict(
    tmp_path: pathlib.Path, value: object
) -> None:
    """The verdict must be a boolean, never a truthy stand-in."""
    document = json.loads(receipt_to_json(_receipt(tmp_path)))
    document["verification_passed"] = value
    with pytest.raises(ProverToolError, match="must be a boolean"):
        parse_receipt(json.dumps(document).encode("ascii"))


def test_parsing_refuses_a_repeated_check(tmp_path: pathlib.Path) -> None:
    """Repeating a check would inflate the recorded evidence."""
    document = json.loads(receipt_to_json(_receipt(tmp_path)))
    document["checks"] = ["manifest-digest", "manifest-digest"]
    with pytest.raises(ProverToolError, match="must not repeat"):
        parse_receipt(json.dumps(document).encode("ascii"))


@given(run_id=_RUN_IDS, run_attempt=_RUN_IDS)
@settings(max_examples=25, deadline=None)
def test_a_receipt_round_trips_for_any_run_identity(
    run_id: int, run_attempt: int
) -> None:
    """Canonical rendering never loses the run that produced the evidence."""
    receipt = ValidationReceipt(
        schema_version=RECEIPT_SCHEMA_VERSION,
        run_id=run_id,
        run_attempt=run_attempt,
        tag=fixtures.TAG,
        manifest_sha256="c" * 64,
        assets=("backend.tar.gz", "inventory.json"),
        verification_passed=True,
        checks=_CHECKS,
    )
    assert parse_receipt(receipt_to_json(receipt)) == receipt
