"""End-to-end CLI contracts for the isolated snapshot commands."""

from __future__ import annotations

import typing as typ

from rust_prover_tools.cli import app
from tests.kani_snapshot_fixtures import make_pin, pin_bytes

if typ.TYPE_CHECKING:
    from pathlib import Path

    import pytest


def _write_pin(tmp_path: Path) -> Path:
    """Write a valid reviewed pin and return its path."""
    path = tmp_path / "snapshot.toml"
    path.write_bytes(pin_bytes(make_pin()))
    return path


def test_check_reports_a_missing_installation(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Checking an empty cache fails without inventing a generation."""
    pin = _write_pin(tmp_path)
    status = app(
        [
            "kani",
            "snapshot",
            "check",
            "--pin",
            str(pin),
            "--cache-dir",
            str(tmp_path / "cache"),
        ],
        exit_on_error=False,
    )
    captured = capsys.readouterr()
    assert status == 1
    assert "not installed" in captured.err


def test_install_without_assets_reports_the_missing_transport(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Installation without a local asset directory fails clearly."""
    pin = _write_pin(tmp_path)
    status = app(
        [
            "kani",
            "snapshot",
            "install",
            "--pin",
            str(pin),
            "--cache-dir",
            str(tmp_path / "cache"),
        ],
        exit_on_error=False,
    )
    captured = capsys.readouterr()
    assert status == 1
    assert "--asset-dir" in captured.err


def test_run_rejects_setup_before_looking_for_an_installation(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A forwarded setup command cannot replace the pinned backend."""
    pin = _write_pin(tmp_path)
    status = app(
        [
            "kani",
            "run",
            "--snapshot-pin",
            str(pin),
            "--cache-dir",
            str(tmp_path / "cache"),
            "--",
            "setup",
        ],
        exit_on_error=False,
    )
    captured = capsys.readouterr()
    assert status == 1
    assert "setup command" in captured.err


def test_run_rejects_the_lean_backend(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The Lean backend is refused before any generation is consulted."""
    pin = _write_pin(tmp_path)
    status = app(
        [
            "kani",
            "run",
            "--snapshot-pin",
            str(pin),
            "--cache-dir",
            str(tmp_path / "cache"),
            "--",
            "-Zlean",
        ],
        exit_on_error=False,
    )
    captured = capsys.readouterr()
    assert status == 1
    assert "Lean backend" in captured.err


def test_snapshot_flags_do_not_inherit_generic_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Generic `INPUT_PIN` selection cannot redirect a snapshot command."""
    pin = _write_pin(tmp_path)
    monkeypatch.setenv("INPUT_PIN", str(tmp_path / "absent.toml"))
    status = app(
        [
            "kani",
            "snapshot",
            "check",
            "--pin",
            str(pin),
            "--cache-dir",
            str(tmp_path / "cache"),
        ],
        exit_on_error=False,
    )
    captured = capsys.readouterr()
    assert status == 1
    assert "not installed" in captured.err
    assert "absent.toml" not in captured.err
