"""Tests for strict snapshot pin parsing and identity keys."""

from __future__ import annotations

import dataclasses as dc
import typing as typ

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_snapshot.cache_key import identity_key
from rust_prover_tools.kani_snapshot.models import SnapshotPin
from rust_prover_tools.kani_snapshot.pins import load_pin, parse_pin, pin_to_toml
from tests.kani_snapshot_fixtures import (
    MANIFEST_SHA256,
    TAG,
    UPSTREAM_COMMIT,
    make_pin,
    other_pin,
    pin_bytes,
)

if typ.TYPE_CHECKING:
    from pathlib import Path


def _render(value: object) -> str:
    """Render one TOML scalar."""
    if isinstance(value, str):
        return f'"{value}"'
    if isinstance(value, bool):
        # TOML booleans are lowercase; `repr(True)` would be a syntax error
        # and would not exercise the integer type check at all.
        return "true" if value else "false"
    return repr(value)


def _document(**overrides: object) -> str:
    """Return a TOML pin with selected fields replaced."""
    values: dict[str, object] = {
        "schema_version": 1,
        "repository": "leynos/rust-prover-tools",
        "tag": TAG,
        "upstream_commit": UPSTREAM_COMMIT,
        "builder_commit": "b" * 40,
        "target": "x86_64-unknown-linux-gnu",
        "manifest_sha256": MANIFEST_SHA256,
    }
    values.update(overrides)
    return (
        "\n".join(f"{key} = {_render(value)}" for key, value in values.items()) + "\n"
    )


def test_valid_pin_parses_and_round_trips() -> None:
    """A complete pin survives rendering and reparsing unchanged."""
    pin = make_pin()
    assert parse_pin(pin_bytes(pin)) == pin
    assert pin_to_toml(pin).endswith(b"\n")


def test_load_pin_reads_exact_bytes(tmp_path: Path) -> None:
    """The file loader applies the same parser as the byte entry point."""
    path = tmp_path / "snapshot.toml"
    path.write_bytes(pin_bytes(make_pin()))
    assert load_pin(path) == make_pin()


def test_load_pin_reports_an_unreadable_file(tmp_path: Path) -> None:
    """A missing pin names the path instead of raising a bare OS error."""
    with pytest.raises(ProverToolError, match="failed to read snapshot pin"):
        load_pin(tmp_path / "absent.toml")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema_version", "1"),
        ("schema_version", "True"),
        ("schema_version", "2"),
        ("repository", "someone/else"),
        ("tag", "kani-snapshot-2026-13-aaaaaaaaaaaa-r1"),
        ("tag", "kani-snapshot-2026-09-bbbbbbbbbbbb-r1"),
        ("tag", "kani-snapshot-2026-09-aaaaaaaaaaaa-r01"),
        ("upstream_commit", "A" * 40),
        ("upstream_commit", "a" * 39),
        ("builder_commit", "z" * 40),
        ("target", "aarch64-unknown-linux-gnu"),
        ("manifest_sha256", "c" * 63),
    ],
)
def test_invalid_pin_fields_fail_closed(field: str, value: str) -> None:
    """Every identity field rejects a value outside the schema."""
    text = _document(**{field: value})
    with pytest.raises(ProverToolError):
        parse_pin(text.encode())


@pytest.mark.parametrize(
    "text",
    [
        "unknown = 1\n" + _document(),
        _document().replace('target = "x86_64-unknown-linux-gnu"\n', ""),
        "not toml",
        _document(schema_version=True),
    ],
)
def test_unknown_missing_or_malformed_pins_fail(text: str) -> None:
    """Unknown keys, missing keys and malformed TOML are rejected."""
    with pytest.raises(ProverToolError):
        parse_pin(text.encode())


def test_boolean_schema_version_is_rejected_as_a_type_error() -> None:
    """A TOML boolean is not an integer, although Python's `bool` is an `int`."""
    with pytest.raises(ProverToolError, match="schema_version must be an integer"):
        parse_pin(_document(schema_version=True).encode())


def test_non_utf8_pin_is_rejected() -> None:
    """A pin must be UTF-8, not arbitrary bytes."""
    with pytest.raises(ProverToolError, match="not valid UTF-8"):
        parse_pin(b"\xff\xfe")


def test_identity_key_covers_every_field() -> None:
    """Changing any identity field changes the cache key."""
    base = make_pin()
    key = identity_key(base)
    assert key == identity_key(base)
    assert len(key) == 64
    for field in (
        "schema_version",
        "repository",
        "tag",
        "upstream_commit",
        "builder_commit",
        "target",
        "manifest_sha256",
    ):
        if field == "schema_version":
            changed = dc.replace(base, schema_version=2)
        elif field == "repository":
            changed = dc.replace(base, repository="other/repo")
        else:
            changed = dc.replace(base, **{field: "0" * len(getattr(base, field))})
        assert identity_key(changed) != key, field


def test_same_reported_version_but_different_identity_has_distinct_keys() -> None:
    """Two snapshots that report one upstream version are still distinct."""
    first = make_pin()
    second = other_pin()
    assert identity_key(first) != identity_key(second)


_HEX40 = st.text(alphabet="0123456789abcdef", min_size=40, max_size=40)
_HEX64 = st.text(alphabet="0123456789abcdef", min_size=64, max_size=64)


@st.composite
def _valid_pins(draw: st.DrawFn) -> SnapshotPin:
    """Generate a valid pin from a random full commit and tag."""
    upstream = draw(_HEX40)
    year = draw(st.integers(min_value=2000, max_value=2099))
    month = draw(st.integers(min_value=1, max_value=12))
    revision = draw(st.integers(min_value=1, max_value=9999))
    return SnapshotPin(
        schema_version=1,
        repository="leynos/rust-prover-tools",
        tag=(f"kani-snapshot-{year:04d}-{month:02d}-{upstream[:12]}-r{revision}"),
        upstream_commit=upstream,
        builder_commit=draw(_HEX40),
        target="x86_64-unknown-linux-gnu",
        manifest_sha256=draw(_HEX64),
    )


@settings(
    max_examples=50,
    deadline=None,
    suppress_health_check=(HealthCheck.too_slow,),
)
@given(pin=_valid_pins())
def test_generated_valid_pins_round_trip(pin: SnapshotPin) -> None:
    """Valid generated pins survive canonical rendering and parsing."""
    assert parse_pin(pin_to_toml(pin)) == pin


@settings(
    max_examples=30,
    deadline=None,
    suppress_health_check=(HealthCheck.too_slow,),
)
@given(pin=_valid_pins(), replacement=_HEX40)
def test_mutating_an_identity_field_changes_the_key(
    pin: SnapshotPin, replacement: str
) -> None:
    """A changed builder commit never shares a cache entry."""
    changed = dc.replace(pin, builder_commit=replacement)
    if changed == pin:
        return
    assert identity_key(pin) != identity_key(changed)
