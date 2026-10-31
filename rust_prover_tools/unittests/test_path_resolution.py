"""Tests for resolving configured CLI paths against a repository root."""

from __future__ import annotations

import typing as typ
from pathlib import Path

from rust_prover_tools.path_resolution import resolve_configured_path

if typ.TYPE_CHECKING:
    import pytest


def test_none_selects_the_default(tmp_path: Path) -> None:
    """An unconfigured path uses the caller's default unchanged."""
    default = tmp_path / "default"

    resolved = resolve_configured_path(None, tmp_path / "repo", default)

    assert resolved == default, "the default path must be returned unchanged"


def test_relative_path_is_resolved_below_the_repository(tmp_path: Path) -> None:
    """A relative path is joined to the repository root."""
    repo_root = tmp_path / "repo"

    resolved = resolve_configured_path(
        Path("cache/z3"),
        repo_root,
        tmp_path / "default",
    )

    assert resolved == repo_root / "cache" / "z3", (
        "a relative path must be resolved below the repository root"
    )


def test_absolute_path_is_kept(tmp_path: Path) -> None:
    """An absolute path is not joined to the repository root."""
    configured = tmp_path / "absolute" / "cache"

    resolved = resolve_configured_path(
        configured,
        tmp_path / "repo",
        tmp_path / "default",
    )

    assert resolved == configured, "an absolute path must be used unchanged"


def test_home_directory_is_expanded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A leading tilde resolves against the user's home directory."""
    monkeypatch.setenv("HOME", str(tmp_path))

    resolved = resolve_configured_path(
        Path("~/cache"),
        tmp_path / "repo",
        tmp_path / "default",
    )

    assert resolved == tmp_path / "cache", "the home directory must be expanded"
