"""Contract tests for CLI evidence-destination handling."""

from __future__ import annotations

import contextlib
import os
import sys
import typing as typ
from pathlib import Path

import pytest

from rust_prover_tools.cli import app
from rust_prover_tools.kani_destinations import (
    open_run_diagnostics,
    require_stdout_free,
    supervision_policy,
)
from rust_prover_tools.kani_harness_cli_options import (
    DestinationFlags,
    PolicyFlags,
)
from tests.kani_harness_cli_support import inventory_file

if typ.TYPE_CHECKING:
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


def test_the_cli_opener_prepares_the_jsonl_boundary(tmp_path: Path) -> None:
    """The CLI path performs the explicit preparation before opening."""
    path = tmp_path / "evidence.jsonl"
    path.write_bytes(b"partial")
    config = supervision_policy(PolicyFlags(), DestinationFlags())

    with contextlib.ExitStack() as stack:
        diagnostics = open_run_diagnostics(
            stack,
            config,
            DestinationFlags(jsonl=path),
        )
        assert diagnostics.jsonl.offset == len(b"partial") + 1

    assert path.read_bytes() == b"partial\n"


def test_a_symlink_alias_is_refused_by_the_cli_adapter(tmp_path: Path) -> None:
    """Resolved paths, not spellings, decide whether two roles collide."""
    target = tmp_path / "evidence.jsonl"
    target.touch()
    alias = tmp_path / "alias.json"
    alias.symlink_to(target)

    with pytest.raises(ValueError, match="distinct"):
        supervision_policy(
            PolicyFlags(),
            DestinationFlags(jsonl=target, report=alias),
        )


def test_a_hard_link_alias_is_refused_by_the_cli_adapter(tmp_path: Path) -> None:
    """Resolved paths differ, but one inode under two names is still shared."""
    first = tmp_path / "first.log"
    first.touch()
    second = tmp_path / "second.log"
    second.hardlink_to(first)

    with pytest.raises(ValueError, match="distinct"):
        supervision_policy(
            PolicyFlags(),
            DestinationFlags(human=first, jsonl=second),
        )


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


class _PipedStdout:
    """A stand-in for `sys.stdout` whose fileno is a real pipe."""

    def __init__(self, descriptor: int) -> None:
        """Remember the write end of the pipe."""
        self._descriptor = descriptor

    def fileno(self) -> int:
        """Return the pipe's write end."""
        return self._descriptor

    def write(self, text: str) -> int:
        """Accept text without buffering it."""
        del text
        return 0

    def flush(self) -> None:
        """Nothing is buffered."""


def test_a_jsonl_alias_of_a_piped_stdout_is_refused() -> None:
    """A pipe has a device and inode, and a descriptor alias names it.

    `/dev/stdout` is `/proc/self/fd/1`, so a path naming the pipe's own
    descriptor has the same identity a piped standard output reports. The
    guard must compare that identity rather than requiring a regular file.
    """
    read_fd, write_fd = os.pipe()
    original = sys.stdout
    try:
        sys.stdout = typ.cast("typ.Any", _PipedStdout(write_fd))
        with pytest.raises(ValueError, match="standard output already names"):
            require_stdout_free(Path(f"/proc/self/fd/{write_fd}"), "--jsonl")
    finally:
        sys.stdout = original
        os.close(read_fd)
        os.close(write_fd)
