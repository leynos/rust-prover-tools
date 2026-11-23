"""Prove the shared input reader reports each unreadable input as an InputError."""

from __future__ import annotations

import typing as typ

import pytest

from rust_prover_tools.cli_support import InputError, read_input_text

if typ.TYPE_CHECKING:
    from pathlib import Path


def test_invalid_utf8_input_names_the_description_and_path(
    tmp_path: Path,
) -> None:
    """Wrap a decode failure and keep the decoder error as the cause."""
    path = tmp_path / "captured.log"
    path.write_bytes(b"\xff\xfe")

    with pytest.raises(InputError) as caught:
        read_input_text(path, "log")

    assert "log" in str(caught.value)
    assert str(path) in str(caught.value)
    assert "not valid UTF-8" in str(caught.value)
    assert isinstance(caught.value.__cause__, UnicodeDecodeError)


def test_a_missing_input_is_an_input_error(tmp_path: Path) -> None:
    """Report a missing file with the common cannot-read wording."""
    path = tmp_path / "absent.log"

    with pytest.raises(InputError, match="cannot read log"):
        read_input_text(path, "log")


def test_a_directory_input_is_an_input_error(tmp_path: Path) -> None:
    """Report a directory handed to a text reader as an unreadable input."""
    with pytest.raises(InputError, match="cannot read log"):
        read_input_text(tmp_path, "log")
