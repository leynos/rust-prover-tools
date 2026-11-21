"""Contract tests for CLI evidence-destination handling."""

from __future__ import annotations

import sys
import typing as typ

import pytest

from rust_prover_tools.cli import app
from rust_prover_tools.kani_harness_cli_options import (
    DestinationFlags,
    PolicyFlags,
    supervision_policy,
)
from tests.kani_harness_cli_support import inventory_file

if typ.TYPE_CHECKING:
    from pathlib import Path

    from rust_prover_tools.kani_harness_run import (
        KaniHarnessPreparation,
        KaniHarnessRunOptions,
    )


def test_shared_human_and_jsonl_destinations_are_refused(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The CLI rejects the collision before opening either handle."""
    shared = tmp_path / "evidence.jsonl"

    status = app([
        "kani",
        "harnesses",
        "run",
        "--installation",
        str(tmp_path / "kani"),
        "--cwd",
        str(tmp_path),
        "--inventory",
        str(inventory_file(tmp_path)),
        "--human",
        str(shared),
        "--jsonl",
        str(shared),
    ])

    assert status == 1
    assert "distinct" in capsys.readouterr().err
    assert not shared.exists()


def test_an_unopenable_destination_is_a_clean_error(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A destination that cannot be opened exits one without a traceback."""
    directory = tmp_path / "evidence.jsonl"
    directory.mkdir()

    def prepared(options: KaniHarnessRunOptions) -> KaniHarnessPreparation:
        """Reach destination opening without qualifying a real installation."""
        del options
        return typ.cast("KaniHarnessPreparation", object())

    monkeypatch.setattr(
        "rust_prover_tools.kani_harness_cli.prepare_harnesses", prepared
    )

    status = app([
        "kani",
        "harnesses",
        "run",
        "--installation",
        str(tmp_path / "kani"),
        "--cwd",
        str(tmp_path),
        "--inventory",
        str(inventory_file(tmp_path)),
        "--jsonl",
        str(directory),
    ])

    assert status == 1
    assert "evidence.jsonl" in capsys.readouterr().err


def test_standard_output_cannot_alias_a_jsonl_destination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Redirecting stdout onto the JSONL file would interleave two grammars."""
    path = tmp_path / "evidence.jsonl"
    path.touch()
    with path.open("a", encoding="utf-8") as handle:
        monkeypatch.setattr(sys, "stdout", handle)
        with pytest.raises(ValueError, match="standard output"):
            supervision_policy(PolicyFlags(), DestinationFlags(jsonl=path))


def test_stdout_is_checked_even_when_human_has_a_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The final table still writes to standard output."""
    path = tmp_path / "evidence.jsonl"
    path.touch()
    with path.open("a", encoding="utf-8") as handle:
        monkeypatch.setattr(sys, "stdout", handle)
        with pytest.raises(ValueError, match="standard output"):
            supervision_policy(
                PolicyFlags(),
                DestinationFlags(
                    human=tmp_path / "human.log",
                    jsonl=path,
                ),
            )
