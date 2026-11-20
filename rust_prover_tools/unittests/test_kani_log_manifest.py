"""Prove the Kani harness manifest validates its document and its records."""

from __future__ import annotations

import json
import typing as typ

import pytest

from rust_prover_tools.kani_log import (
    CLAIM,
    CONTROL,
    MANIFEST_SCHEMA_VERSION,
    Harness,
    KaniManifestError,
    Manifest,
    manifest_from_document,
    parse_manifest,
)

Document = dict[str, typ.Any]


def document(**overrides: object) -> Document:
    """Return a minimal valid manifest document with the given overrides."""
    result: Document = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "harnesses": [{"name": "h::claim", "role": CLAIM}],
    }
    result.update(overrides)
    return result


def harness(**overrides: object) -> Document:
    """Return a minimal valid harness entry with the given overrides."""
    result: Document = {"name": "h::claim", "role": CLAIM}
    result.update(overrides)
    return result


def refused(text: str) -> str:
    """Return the manifest error a text is refused with."""
    with pytest.raises(KaniManifestError) as caught:
        parse_manifest(text)
    return str(caught.value)


def test_a_valid_manifest_reads_its_claims_and_controls() -> None:
    """Read roles, covers and expected failures from a valid document."""
    manifest = parse_manifest(
        json.dumps(
            document(
                harnesses=[
                    harness(covers=["the boundary is reachable"]),
                    harness(
                        name="h::control",
                        role=CONTROL,
                        expected_failure="assertion.1",
                    ),
                ],
            ),
        ),
    )
    assert manifest.names() == ("h::claim", "h::control")
    assert [item.name for item in manifest.claims] == ["h::claim"]
    assert [item.name for item in manifest.controls] == ["h::control"]
    assert manifest.declared_covers() == ("the boundary is reachable",)


def test_malformed_json_is_refused_with_its_location() -> None:
    """Refuse text that is not JSON, naming the line rather than guessing."""
    reason = refused("{not json")
    assert "Kani harness manifest" in reason
    assert "not valid JSON" in reason


def test_a_duplicated_json_key_is_refused() -> None:
    """Refuse a duplicated key rather than letting the last value win."""
    text = (
        '{"schema_version": 1, "schema_version": 1, '
        '"harnesses": [{"name": "h::c", "role": "claim"}]}'
    )
    assert "repeats the key 'schema_version'" in refused(text)


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_a_non_finite_constant_is_refused(constant: str) -> None:
    """Refuse JSON extensions that would produce an unreadable number."""
    text = json.dumps(document()).replace("1", constant, 1)
    assert f"non-finite constant {constant}" in refused(text)


def test_a_non_object_document_is_refused() -> None:
    """Require the manifest to be a JSON object."""
    assert "must be a JSON object" in refused("[]")


def test_an_unknown_top_level_key_is_refused() -> None:
    """Refuse a key the manifest does not define."""
    assert "unknown keys" in refused(json.dumps(document(revision="0.1")))


@pytest.mark.parametrize("version", [None, 0, 2, True, "1"])
def test_an_unsupported_schema_version_is_refused(version: object) -> None:
    """Require exactly the supported schema version, with a truthful type."""
    entry = document()
    entry["schema_version"] = version
    assert "must declare schema_version" in refused(json.dumps(entry))


def test_a_missing_harnesses_array_is_refused() -> None:
    """Require a harnesses array rather than treating absence as empty."""
    entry = document()
    del entry["harnesses"]
    assert "has no harnesses array" in refused(json.dumps(entry))


def test_an_empty_manifest_is_refused() -> None:
    """Refuse a manifest that declares nothing to analyse."""
    assert "declares no harnesses" in refused(
        json.dumps(document(harnesses=[])),
    )


def test_an_unknown_harness_key_is_refused() -> None:
    """Refuse a key the harness entry does not define."""
    manifest = document(harnesses=[harness(expected="assertion.1")])
    assert "unknown keys" in refused(json.dumps(manifest))


def test_a_duplicate_harness_name_is_refused() -> None:
    """Refuse a manifest that declares one harness twice."""
    manifest = document(
        harnesses=[harness(), harness(name="h::claim")],
    )
    assert "declares a harness twice" in refused(json.dumps(manifest))


def test_an_unknown_role_is_refused() -> None:
    """Refuse a role outside claim and control."""
    manifest = document(harnesses=[harness(role="witness")])
    assert "unknown role 'witness'" in refused(json.dumps(manifest))


def test_a_control_without_an_expected_failure_is_refused() -> None:
    """Require a control to name the property it is supposed to fail."""
    manifest = document(harnesses=[harness(role=CONTROL)])
    assert "names no property it is expected to fail" in refused(
        json.dumps(manifest),
    )


def test_a_control_cannot_declare_should_panic() -> None:
    """Refuse the panic flag on a negative control."""
    manifest = document(
        harnesses=[
            harness(
                role=CONTROL,
                expected_failure="assertion.1",
                should_panic=True,
            ),
        ],
    )
    assert "should_panic" in refused(json.dumps(manifest))


def test_a_should_panic_claim_cannot_declare_covers() -> None:
    """Refuse covers a panic claim could never reach."""
    manifest = document(
        harnesses=[harness(should_panic=True, covers=["unreachable"])],
    )
    assert "covers would be unreachable" in refused(json.dumps(manifest))


@pytest.mark.parametrize(
    ("entry", "message"),
    [
        (harness(name=True), "does not name a harness and a role"),
        (harness(covers="a cover"), "malformed covers list"),
        (harness(covers=[True]), "malformed covers list"),
        (harness(expected_failure=True), "malformed expected_failure"),
        (harness(should_panic="false"), "malformed should_panic"),
        ("not an object", "is not an object"),
    ],
)
def test_an_entry_with_the_wrong_type_is_refused(
    entry: object,
    message: str,
) -> None:
    """Refuse booleans, strings and scalars where the schema requires another type."""
    manifest = document(harnesses=[entry])
    assert message in refused(json.dumps(manifest))


def test_a_direct_harness_refuses_an_invalid_role() -> None:
    """Apply the same role rules to a directly constructed record."""
    with pytest.raises(KaniManifestError, match="unknown role"):
        Harness("h::x", "witness")


def test_a_direct_manifest_refuses_a_duplicate_name() -> None:
    """Apply the same uniqueness rule to a directly constructed manifest."""
    with pytest.raises(KaniManifestError, match="twice"):
        Manifest((Harness("h::c", CLAIM), Harness("h::c", CLAIM)))


def test_select_keeps_the_manifest_order() -> None:
    """Narrow a manifest to the requested names, preserving request order."""
    manifest = manifest_from_document(
        document(
            harnesses=[
                harness(name="h::a"),
                harness(name="h::b"),
                harness(name="h::k", role=CONTROL, expected_failure="assertion.1"),
            ],
        ),
    )
    selected = manifest.select(("h::k", "h::a"))
    assert selected.names() == ("h::k", "h::a")


def test_select_refuses_an_undeclared_name() -> None:
    """Refuse a selection naming a harness the manifest does not declare."""
    manifest = manifest_from_document(document())
    with pytest.raises(KaniManifestError, match="does not declare"):
        manifest.select(("h::missing",))


def test_select_refuses_a_duplicate_name() -> None:
    """Refuse a selection naming one harness twice."""
    manifest = manifest_from_document(document())
    with pytest.raises(KaniManifestError, match="twice"):
        manifest.select(("h::claim", "h::claim"))
