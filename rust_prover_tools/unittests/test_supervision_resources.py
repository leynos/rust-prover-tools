"""Hold the procfs readers and the resource probe to their values and absences."""

from __future__ import annotations

import os
import typing as typ

import pytest

from rust_prover_tools.supervision import (
    ProcfsResourceProbe,
    read_meminfo,
    sum_group_rss,
)

if typ.TYPE_CHECKING:
    from pathlib import Path

from rust_prover_tools.unittests.supervision_doubles import (
    VirtualClock,
    spec,
)

_PAGE = os.sysconf("SC_PAGE_SIZE")


def _stat(pid: int, pgrp: int, rss_pages: int, name: str = "child") -> str:
    """Build a procfs ``stat`` line with the fields this module reads.

    Parameters
    ----------
    pid : int
        Process identifier.
    pgrp : int
        Process-group identifier.
    rss_pages : int
        Resident set size in pages.
    name : str
        Command name, placed in parentheses.

    Returns
    -------
    str
        A stat line with the process group at field five and RSS at field
        twenty-four.
    """
    fields = ["0"] * 50
    fields[0] = "S"
    fields[2] = str(pgrp)
    fields[21] = str(rss_pages)
    return f"{pid} ({name}) " + " ".join(fields)


def _proc(tmp_path: Path, *, with_meminfo: bool = True) -> Path:
    """Create a fake procfs root."""
    root = tmp_path / "proc"
    root.mkdir()
    if with_meminfo:
        (root / "meminfo").write_text(
            "MemTotal:       16384000 kB\n"
            "MemFree:         2048000 kB\n"
            "MemAvailable:    8388608 kB\n",
            encoding="utf-8",
        )
    return root


def test_group_rss_sums_only_the_owned_group(tmp_path: Path) -> None:
    """Members of other groups are not part of this harness's measurement."""
    proc = _proc(tmp_path)
    for pid, pgrp, rss in ((1234, 1234, 100), (5678, 1234, 50), (9999, 9999, 999)):
        child = proc / str(pid)
        child.mkdir()
        (child / "stat").write_text(_stat(pid, pgrp, rss), encoding="utf-8")

    measurement = sum_group_rss(1234, proc=proc)

    assert measurement.value == (100 + 50) * _PAGE


def test_group_rss_reports_an_unreadable_procfs(tmp_path: Path) -> None:
    """A host without procfs yields an explained absence, not a zero."""
    measurement = sum_group_rss(1234, proc=tmp_path / "missing")

    assert measurement.value is None
    assert measurement.unavailable_reason is not None
    assert "cannot be read" in measurement.unavailable_reason


def test_group_rss_skips_a_process_that_disappears(tmp_path: Path) -> None:
    """A process that exits between listing and reading is not a failure."""
    proc = _proc(tmp_path)
    child = proc / "1234"
    child.mkdir()
    (child / "stat").write_text(_stat(1234, 1234, 10), encoding="utf-8")
    gone = proc / "5678"
    gone.mkdir()
    (gone / "stat").write_text("not a stat line", encoding="utf-8")

    assert sum_group_rss(1234, proc=proc).value == 10 * _PAGE


def test_meminfo_values_are_reported_in_bytes(tmp_path: Path) -> None:
    """Kibibyte fields from meminfo become byte measurements."""
    values = read_meminfo(_proc(tmp_path) / "meminfo")

    assert values["MemTotal"] == 16_384_000 * 1024
    assert values["MemAvailable"] == 8_388_608 * 1024


def test_the_probe_reports_a_sample_with_identity_and_time(tmp_path: Path) -> None:
    """A probe sample carries its harness, elapsed time, and values."""
    proc = _proc(tmp_path)
    child = proc / "1234"
    child.mkdir()
    (child / "stat").write_text(_stat(1234, 1234, 10), encoding="utf-8")
    clock = VirtualClock()
    clock.advance(2.5)
    probe = ProcfsResourceProbe(clock=clock, proc=proc, meminfo=proc / "meminfo")

    sample = probe.sample(
        spec(),
        group_id=1234,
        started_at=clock.now(),
        started_monotonic=0.0,
    )

    assert sample.package == "example"
    assert sample.harness == "harnesses::verified"
    assert sample.elapsed_seconds == pytest.approx(2.5)
    assert sample.group_rss.value == 10 * _PAGE
    assert sample.host_available.value == 8_388_608 * 1024
    assert sample.host_total.value == 16_384_000 * 1024


def test_the_probe_explains_a_missing_pid_and_meminfo(tmp_path: Path) -> None:
    """Absence is explicit for each value that could not be read."""
    probe = ProcfsResourceProbe(
        clock=VirtualClock(),
        proc=tmp_path / "missing",
        meminfo=tmp_path / "missing-meminfo",
    )

    sample = probe.sample(
        spec(),
        group_id=None,
        started_at=VirtualClock().now(),
        started_monotonic=0.0,
    )

    assert sample.group_rss.unavailable_reason == (
        "the launcher reported no child process id"
    )
    assert sample.host_available.value is None
    assert sample.host_total.value is None


def test_a_non_linux_host_is_refused() -> None:
    """The measurement layer states its platform boundary."""
    from unittest import mock

    from rust_prover_tools.supervision import ensure_linux

    with (
        mock.patch("sys.platform", "darwin"),
        pytest.raises(NotImplementedError, match="Linux"),
    ):
        ensure_linux()


def test_the_probe_refuses_a_non_linux_platform() -> None:
    """Construction is where an unsupported platform is named."""
    from unittest import mock

    with (
        mock.patch("sys.platform", "darwin"),
        pytest.raises(NotImplementedError, match="Linux"),
    ):
        ProcfsResourceProbe(clock=VirtualClock())


def test_group_rss_without_a_member_is_unavailable(tmp_path: Path) -> None:
    """An empty group is not a measured zero."""
    proc = _proc(tmp_path)
    child = proc / "1234"
    child.mkdir()
    (child / "stat").write_text(_stat(1234, 9999, 10), encoding="utf-8")

    measurement = sum_group_rss(1234, proc=proc)

    assert measurement.value is None
    assert "no process in the group" in (measurement.unavailable_reason or "")
