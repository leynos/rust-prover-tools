"""Hold the Linux resource readers to their values and their absences."""

from __future__ import annotations

import os
import typing as typ

import pytest

from rust_prover_tools.supervision import (
    CgroupV2Reader,
    Measurement,
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


def _cgroup(tmp_path: Path, *, peak: bool = True) -> tuple[Path, Path]:
    """Create a fake cgroup v2 mount and membership file."""
    mount = tmp_path / "sys" / "fs" / "cgroup"
    job = mount / "user.slice" / "job.scope"
    job.mkdir(parents=True)
    (job / "memory.current").write_text("67108864\n", encoding="utf-8")
    if peak:
        (job / "memory.peak").write_text("134217728\n", encoding="utf-8")
    (job / "memory.events").write_text(
        "low 0\nhigh 0\nmax 2\noom 0\noom_kill 3\n", encoding="utf-8"
    )
    membership = tmp_path / "self-cgroup"
    membership.write_text("0::/user.slice/job.scope\n", encoding="utf-8")
    return mount, membership


def test_cgroup_discovery_and_memory_reads(tmp_path: Path) -> None:
    """A discovered path yields current, peak, and oom_kill values."""
    mount, membership = _cgroup(tmp_path)
    reader = CgroupV2Reader(root=mount, self_cgroup=membership)

    path = reader.discover()
    memory = reader.read(path)

    assert path == "/user.slice/job.scope"
    assert memory.current.value == 64 * 1024 * 1024
    assert memory.peak.value == 128 * 1024 * 1024
    assert memory.oom_kill.value == 3


def test_a_missing_peak_file_is_an_explicit_absence(tmp_path: Path) -> None:
    """Older kernels do not provide ``memory.peak``."""
    mount, membership = _cgroup(tmp_path, peak=False)
    reader = CgroupV2Reader(root=mount, self_cgroup=membership)

    memory = reader.read(reader.discover())

    assert memory.peak.value is None
    assert memory.peak.unavailable_reason is not None
    assert memory.current.value == 64 * 1024 * 1024


def test_a_missing_cgroup_is_reported_without_raising(tmp_path: Path) -> None:
    """A host with no cgroup v2 path still produces an observation."""
    reader = CgroupV2Reader(
        root=tmp_path / "missing",
        self_cgroup=tmp_path / "missing-membership",
    )

    memory = reader.read(reader.discover())

    assert memory.current.value is None
    assert memory.peak.value is None
    assert memory.oom_kill.value is None


def test_the_oom_kill_delta_is_separate_from_the_watchdog_stop() -> None:
    """A policy stop is never presented as proof of a kernel OOM kill."""
    from rust_prover_tools.supervision import CgroupMemory, CgroupObservation

    def memory(oom: int) -> CgroupMemory:
        """Return one cgroup memory read with the given oom_kill count."""
        return CgroupMemory(
            current=Measurement.available(1),
            peak=Measurement.available(2),
            oom_kill=Measurement.available(oom),
        )

    observation = CgroupObservation(
        path="/user.slice/job.scope",
        start=memory(3),
        finish=memory(5),
    )

    assert observation.oom_kill_delta == 2


@pytest.mark.parametrize("reason", ["no path", "unreadable"])
def test_an_absent_cgroup_has_no_delta(reason: str) -> None:
    """An absent or unreadable observation reports no delta."""
    measurement = Measurement.unavailable(reason)
    from rust_prover_tools.supervision import CgroupMemory, CgroupObservation

    absent = CgroupMemory(current=measurement, peak=measurement, oom_kill=measurement)
    observation = CgroupObservation(path=None, start=absent, finish=absent)

    assert observation.oom_kill_delta is None


def test_a_non_linux_host_is_refused() -> None:
    """The measurement layer states its platform boundary."""
    from unittest import mock

    from rust_prover_tools.supervision import ensure_linux

    with (
        mock.patch("sys.platform", "darwin"),
        pytest.raises(NotImplementedError, match="Linux"),
    ):
        ensure_linux()
