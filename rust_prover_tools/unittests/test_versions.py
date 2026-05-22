"""Tests for version and checksum parsing helpers."""

from __future__ import annotations

import typing as typ

import pytest
from hypothesis import given
from hypothesis import strategies as st

from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.versions import (
    expected_sha256,
    parse_tool_version,
    parse_verus_toolchain,
    read_version_pin,
    validate_semver,
)

if typ.TYPE_CHECKING:
    from pathlib import Path


def test_read_version_pin_trims_and_validates(tmp_path: Path) -> None:
    """Version pins are read as trimmed semantic versions."""
    version_file = tmp_path / "VERSION"
    version_file.write_text(" 1.2.3\n", encoding="utf-8")

    assert read_version_pin(version_file) == "1.2.3", (
        "Version pin should be trimmed of whitespace"
    )


def test_read_version_pin_rejects_missing_file(tmp_path: Path) -> None:
    """Missing version files produce user-facing errors."""
    with pytest.raises(ProverToolError, match="does not exist"):
        read_version_pin(tmp_path / "VERSION")


def test_parse_tool_version_finds_first_semver() -> None:
    """Tool version parsing accepts surrounding text."""
    assert parse_tool_version("Kani verifier 0.61.0\n", tool_name="Kani") == "0.61.0", (
        "Should extract first semver from tool output"
    )


def test_expected_sha256_uses_archive_column(tmp_path: Path) -> None:
    """Checksum lookup matches the archive column exactly."""
    checksum_file = tmp_path / "SHA256SUMS"
    checksum_file.write_text(
        "abc123 verus-1.0.0-x86-linux.zip\ndef456 verus-1.0.0-aarch64-linux.zip\n",
        encoding="utf-8",
    )

    assert expected_sha256(checksum_file, "verus-1.0.0-x86-linux.zip") == "abc123", (
        "Should match checksum by archive name"
    )


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        (
            "Verus\nToolchain: nightly-2025-01-01-x86_64-unknown-linux-gnu\n",
            "nightly-2025-01-01-x86_64-unknown-linux-gnu",
        ),
        (
            "error: required rust toolchain 1.88.0-x86_64-unknown-linux-gnu is missing",
            "1.88.0-x86_64-unknown-linux-gnu",
        ),
    ],
)
def test_parse_verus_toolchain_supported_formats(
    output: str,
    expected: str,
) -> None:
    """Verus toolchain parsing supports both script-recognized formats."""
    assert parse_verus_toolchain(output) == expected, (
        "Should parse toolchain from both recognised formats"
    )


@given(
    major=st.integers(min_value=0, max_value=9999),
    minor=st.integers(min_value=0, max_value=9999),
    patch=st.integers(min_value=0, max_value=9999),
)
def test_validate_semver_accepts_numeric_triples(
    major: int,
    minor: int,
    patch: int,
) -> None:
    """Numeric major/minor/patch triples are accepted."""
    version = f"{major}.{minor}.{patch}"

    assert validate_semver(version) == version, (
        "Numeric major.minor.patch triples should be accepted"
    )


@given(st.text().filter(lambda value: value.count(".") != 2))
def test_validate_semver_rejects_values_without_three_segments(value: str) -> None:
    """Values without three dot-separated segments are rejected."""
    with pytest.raises(ProverToolError):
        validate_semver(value)
