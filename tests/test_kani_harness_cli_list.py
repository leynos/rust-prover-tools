"""Contract tests for `kani harnesses list`."""

from __future__ import annotations

import json
import typing as typ

from rust_prover_tools.cli import app
from tests.kani_harness_cli_support import inventory_file

if typ.TYPE_CHECKING:
    from pathlib import Path

    import pytest


def test_list_prints_the_inventory_json(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`list` prints deterministic JSON for the resolved inventory."""
    path = inventory_file(tmp_path)

    status = app(["kani", "harnesses", "list", "--inventory", str(path)])

    assert status == 0
    document = json.loads(capsys.readouterr().out)
    assert [package["package"] for package in document["packages"]] == [
        "alpha",
        "beta",
    ]


def test_list_filters_by_package(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A package filter narrows the inventory before it is printed."""
    path = inventory_file(tmp_path)

    status = app([
        "kani",
        "harnesses",
        "list",
        "--inventory",
        str(path),
        "--package",
        "beta",
    ])

    assert status == 0
    document = json.loads(capsys.readouterr().out)
    assert [package["package"] for package in document["packages"]] == ["beta"]


def test_list_refuses_an_unknown_package(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An unknown package is an expected error with a nonzero status."""
    path = inventory_file(tmp_path)

    status = app([
        "kani",
        "harnesses",
        "list",
        "--inventory",
        str(path),
        "--package",
        "missing",
    ])

    assert status == 1
    assert "does not contain" in capsys.readouterr().err


def test_list_writes_to_a_file(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`--output` writes the same JSON without printing it."""
    path = inventory_file(tmp_path)
    output = tmp_path / "nested" / "inventory.json"

    status = app([
        "kani",
        "harnesses",
        "list",
        "--inventory",
        str(path),
        "--output",
        str(output),
    ])

    assert status == 0
    assert not capsys.readouterr().out
    assert json.loads(output.read_text(encoding="utf-8"))["schema"] == 1


def test_list_ignores_the_generic_input_environment(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The harnesses subtree disables INPUT_* binding."""
    path = inventory_file(tmp_path)
    monkeypatch.setenv("INPUT_INVENTORY", str(tmp_path / "wrong.json"))

    status = app(["kani", "harnesses", "list", "--inventory", str(path)])

    assert status == 0
    assert json.loads(capsys.readouterr().out)["schema"] == 1
