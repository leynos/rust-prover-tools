"""Tests for strict snapshot manifest parsing and binding."""

from __future__ import annotations

import json
import typing as typ

import pytest

from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_snapshot.manifest import (
    manifest_digest,
    manifest_to_json,
    parse_manifest,
    validate_binding,
)
from tests.kani_snapshot_fixtures import (
    make_manifest,
    make_pin,
    manifest_bytes,
    with_manifest,
)


def _valid_document() -> dict[str, typ.Any]:
    """Return a parsed valid manifest document."""
    return typ.cast("dict[str, typ.Any]", json.loads(manifest_bytes(make_manifest())))


def _encode(document: object) -> bytes:
    """Encode a document as JSON bytes."""
    return json.dumps(document).encode()


def test_valid_manifest_round_trips() -> None:
    """A complete manifest survives canonical rendering and parsing."""
    manifest = make_manifest()
    parsed = parse_manifest(manifest_to_json(manifest))
    assert parsed.manifest == manifest
    assert parsed.raw == manifest_to_json(manifest)
    assert parsed.sha256 == manifest_digest(manifest_to_json(manifest))


def test_rendering_is_deterministic_and_sorted() -> None:
    """Rendering the same model is byte-stable and starts with the first key."""
    first = manifest_to_json(make_manifest())
    second = manifest_to_json(make_manifest())
    assert first == second
    assert first.startswith(b'{\n  "assets"')
    assert first.endswith(b"\n")


def test_duplicate_object_keys_are_rejected() -> None:
    """A repeated JSON object key is refused rather than last-one-wins."""
    with pytest.raises(ProverToolError, match="duplicate key"):
        parse_manifest(b'{"tag": "a", "tag": "b"}')


def test_non_finite_numbers_are_rejected() -> None:
    """NaN and Infinity are not valid manifest numbers."""
    data = manifest_bytes(make_manifest()).replace(
        b'"wall_seconds": 1800.0', b'"wall_seconds": NaN'
    )
    assert data != manifest_bytes(make_manifest())
    with pytest.raises(ProverToolError, match="non-finite"):
        parse_manifest(data)


def test_unknown_top_level_key_is_rejected() -> None:
    """An unknown top-level key fails closed."""
    document = _valid_document()
    document["surprise"] = 1
    with pytest.raises(ProverToolError, match="unknown keys"):
        parse_manifest(_encode(document))


def test_missing_top_level_key_is_rejected() -> None:
    """A missing required group fails closed."""
    document = _valid_document()
    del document["runtime"]
    with pytest.raises(ProverToolError, match="missing keys"):
        parse_manifest(_encode(document))


@pytest.mark.parametrize(
    ("group", "key", "value"),
    [
        ("workflow", "run_id", 0),
        ("workflow", "run_attempt", "1"),
        ("toolchain", "identifier", ""),
        ("backend", "revision", "d" * 39),
        ("runtime", "glibc_baseline", 2.35),
        ("build", "recipe_digest", "9" * 63),
        ("cache", "outcome", ""),
        ("resources", "wall_seconds", "slow"),
        ("launcher", "package_version", 67),
    ],
)
def test_invalid_group_fields_fail_closed(group: str, key: str, value: object) -> None:
    """Wrong types and malformed identities in nested groups are rejected."""
    document = _valid_document()
    typ.cast("dict[str, typ.Any]", document[group])[key] = value
    with pytest.raises(ProverToolError):
        parse_manifest(_encode(document))


@pytest.mark.parametrize(
    "assets",
    [
        [],
        [
            {
                "role": "backend",
                "name": "a",
                "media_type": "x",
                "size": 1,
                "sha256": "1" * 64,
            }
        ],
        [
            {
                "role": role,
                "name": "same",
                "media_type": "x",
                "size": 1,
                "sha256": "1" * 64,
            }
            for role in ("backend", "launcher", "notices", "inventory")
        ],
        [
            {
                "role": "unknown",
                "name": "a",
                "media_type": "x",
                "size": 1,
                "sha256": "1" * 64,
            },
            {
                "role": "launcher",
                "name": "b",
                "media_type": "x",
                "size": 1,
                "sha256": "1" * 64,
            },
            {
                "role": "notices",
                "name": "c",
                "media_type": "x",
                "size": 1,
                "sha256": "1" * 64,
            },
            {
                "role": "inventory",
                "name": "d",
                "media_type": "x",
                "size": 1,
                "sha256": "1" * 64,
            },
        ],
    ],
)
def test_inconsistent_asset_inventories_are_rejected(
    assets: list[dict[str, object]],
) -> None:
    """Exactly one uniquely named asset per role is required."""
    document = _valid_document()
    document["assets"] = assets
    with pytest.raises(ProverToolError):
        parse_manifest(_encode(document))


def test_non_utf8_manifest_is_rejected() -> None:
    """A manifest must be valid UTF-8."""
    with pytest.raises(ProverToolError, match="not valid UTF-8"):
        parse_manifest(b"\xff\xfe")


def test_invalid_json_is_rejected() -> None:
    """Malformed JSON is reported, not partially accepted."""
    with pytest.raises(ProverToolError, match="not valid JSON"):
        parse_manifest(b"{")


def test_oversized_integer_measurement_fails_cleanly() -> None:
    """An integer beyond the float range is a diagnostic, not a traceback."""
    document = _valid_document()
    typ.cast("dict[str, typ.Any]", document["resources"])["wall_seconds"] = 10**400
    with pytest.raises(ProverToolError, match="must be finite"):
        parse_manifest(_encode(document))


def test_over_limit_integer_digits_are_reported() -> None:
    """CPython's integer-digit guard is converted into a clean failure."""
    data = b'{"value": ' + b"9" * 5000 + b"}"
    with pytest.raises(ProverToolError, match="not valid JSON"):
        parse_manifest(data)


def test_deeply_nested_json_is_reported() -> None:
    """Hostile nesting depth becomes a clean failure, not a traceback."""
    nested = b"[" * 100_000 + b"]" * 100_000
    with pytest.raises(ProverToolError, match="not valid JSON"):
        parse_manifest(nested)


@pytest.mark.parametrize(
    "name",
    ["../backend.tar.gz", "a/b", ".", "manifest.json", "SHA256SUMS"],
)
def test_unsafe_asset_names_are_rejected(name: str) -> None:
    """Asset names must be plain basenames that do not shadow release files."""
    document = _valid_document()
    typ.cast("list[dict[str, object]]", document["assets"])[0]["name"] = name
    with pytest.raises(ProverToolError, match="not a safe release asset name"):
        parse_manifest(_encode(document))


def test_digest_covers_exact_bytes_including_newline() -> None:
    """A trailing-byte change changes the manifest digest."""
    data = manifest_bytes(make_manifest())
    assert manifest_digest(data) != manifest_digest(data + b"\n")


def test_binding_accepts_a_matching_pin() -> None:
    """A manifest bound to its pin validates."""
    manifest = make_manifest()
    pin = with_manifest(make_pin(), manifest)
    validate_binding(pin, manifest)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("tag", "kani-snapshot-2026-09-bbbbbbbbbbbb-r2"),
        ("upstream_commit", "b" * 40),
        ("builder_commit", "e" * 40),
    ],
)
def test_binding_rejects_a_different_identity(field: str, value: str) -> None:
    """Any identity disagreement is refused."""
    manifest = make_manifest(**{field: value})
    with pytest.raises(ProverToolError, match="does not match"):
        validate_binding(make_pin(), manifest)
