"""Contract tests for `kani harnesses run`."""

from __future__ import annotations

import io
import sys
import typing as typ

import pytest

from rust_prover_tools.cli import app
from rust_prover_tools.kani_harness_cli_options import (
    DestinationFlags,
    PolicyFlags,
    supervision_policy,
)
from rust_prover_tools.kani_harness_run import (
    KaniHarnessPreparation,
    KaniHarnessRun,
    KaniHarnessRunOptions,
    LegacyRoute,
    SnapshotRoute,
)
from rust_prover_tools.kani_inventory import SelectionError
from rust_prover_tools.supervision import ExecutionStatus
from tests.kani_harness_cli_support import (
    capture_run,
    inventory_file,
    run,
)

if typ.TYPE_CHECKING:
    from pathlib import Path


def test_run_requires_exactly_one_route(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Neither or both routes is a usage error."""
    assert app(["kani", "harnesses", "run"]) == 1
    assert "exactly one" in capsys.readouterr().err

    assert (
        app([
            "kani",
            "harnesses",
            "run",
            "--installation",
            str(tmp_path / "kani"),
            "--cwd",
            str(tmp_path),
            "--snapshot-pin",
            str(tmp_path / "pin.toml"),
        ])
        == 1
    )
    assert "exactly one" in capsys.readouterr().err


def test_the_legacy_route_requires_a_cwd(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The legacy route has no implicit proof directory in this CLI."""
    status = app([
        "kani",
        "harnesses",
        "run",
        "--installation",
        str(tmp_path / "kani"),
        "--inventory",
        str(inventory_file(tmp_path)),
    ])

    assert status == 1
    assert "--cwd is required" in capsys.readouterr().err


def test_run_passing_returns_zero_and_prints_the_table(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A complete passing run exits zero and shows its table."""
    inventory = inventory_file(tmp_path)
    captured = capture_run(monkeypatch, run())

    status = app([
        "kani",
        "harnesses",
        "run",
        "--installation",
        str(tmp_path / "kani"),
        "--cwd",
        str(tmp_path),
        "--inventory",
        str(inventory),
        "--package",
        "alpha",
        "--harness",
        "harnesses::verified",
        "--extra-arg=--unwind",
        "--extra-arg",
        "2",
        "--report",
        str(tmp_path / "report.json"),
        "--jsonl",
        str(tmp_path / "evidence.jsonl"),
    ])

    assert status == 0
    output = capsys.readouterr().out
    assert "PACKAGE" in output
    assert "harnesses::verified" in output
    options = captured[0].options
    assert isinstance(options.route, LegacyRoute)
    assert options.route.arguments == ("--unwind", "2")
    assert options.packages == ("alpha",)
    assert options.harnesses == ("harnesses::verified",)
    assert options.config.sinks.report_path == tmp_path / "report.json"
    assert captured[0].dependencies.interrupts is not None


def test_run_snapshot_route_is_selected(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The snapshot route loads its pin and carries the cache directory."""
    from rust_prover_tools.kani_snapshot.models import SnapshotPin

    pin = SnapshotPin(
        schema_version=1,
        repository="leynos/rust-prover-tools",
        tag="kani-snapshot-2026-11-abcdef123456-r1",
        upstream_commit="a" * 40,
        builder_commit="b" * 40,
        target="x86_64-unknown-linux-gnu",
        manifest_sha256="c" * 64,
    )
    captured = capture_run(monkeypatch, run())
    monkeypatch.setattr(
        "rust_prover_tools.kani_harness_cli_options.load_pin", lambda path: pin
    )

    status = app([
        "kani",
        "harnesses",
        "run",
        "--snapshot-pin",
        str(tmp_path / "snapshot.toml"),
        "--cache-dir",
        str(tmp_path / "cache"),
        "--inventory",
        str(inventory_file(tmp_path)),
    ])

    assert status == 0
    route = captured[0].options.route
    assert isinstance(route, SnapshotRoute)
    assert route.pin is pin
    assert route.cache_home == tmp_path / "cache"


def test_run_failure_returns_one(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A completed run with a failure exits nonzero."""
    capture_run(monkeypatch, run(status=ExecutionStatus.FAILED))

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
    ])

    assert status == 1
    assert "failed" in capsys.readouterr().out


def test_run_interruption_preserves_the_signal_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A signal status is `128 + signal`, not a generic failure."""
    capture_run(monkeypatch, run(interrupt_signal=15))

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
    ])

    assert status == 128 + 15


def test_run_expected_error_returns_one(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A selector conflict is reported once, on standard error."""

    def fail(options: KaniHarnessRunOptions) -> KaniHarnessRun:
        """Refuse the run as a selector conflict."""
        del options
        raise SelectionError("conflict")

    monkeypatch.setattr("rust_prover_tools.kani_harness_cli.prepare_harnesses", fail)

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
    ])

    assert status == 1
    assert "conflict" in capsys.readouterr().err


def test_a_closed_stdout_does_not_replace_a_passing_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reader that goes away loses output, not the run's status."""

    class _Closed(io.StringIO):
        """A text stream whose reader has gone away."""

        def write(self, payload: str, /) -> int:
            """Refuse the write, as a closed pipe does."""
            raise BrokenPipeError

    capture_run(monkeypatch, run())
    monkeypatch.setattr("sys.stdout", _Closed())

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
    ])

    assert status == 0


def test_a_jsonl_append_keeps_absolute_offsets(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reopened JSONL file does not report offsets from zero."""
    jsonl = tmp_path / "evidence.jsonl"
    jsonl.write_text("existing-record\n", encoding="utf-8")
    captured = capture_run(monkeypatch, run())

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
        str(jsonl),
    ])

    assert status == 0
    diagnostics = captured[0].dependencies.diagnostics
    assert diagnostics is not None
    assert diagnostics.jsonl.offset == len("existing-record\n")


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


def test_the_legacy_route_refuses_a_snapshot_cache_dir(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A snapshot-only option must not be silently ignored."""
    status = app([
        "kani",
        "harnesses",
        "run",
        "--installation",
        str(tmp_path / "kani"),
        "--cwd",
        str(tmp_path),
        "--cache-dir",
        str(tmp_path / "cache"),
    ])

    assert status == 1
    assert "--cache-dir is only valid" in capsys.readouterr().err


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
