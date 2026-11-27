"""Hold the cgroup v2 reader to its values, hierarchy, and absences."""

from __future__ import annotations

import typing as typ

import pytest

from rust_prover_tools.supervision import CgroupV2Reader, Measurement

if typ.TYPE_CHECKING:
    from pathlib import Path


def _cgroup(tmp_path: Path, *, peak: bool = True) -> tuple[Path, Path]:
    """Create a fake cgroup v2 mount and membership file."""
    mount = tmp_path / "sys" / "fs" / "cgroup"
    job = mount / "user.slice" / "job.scope"
    job.mkdir(parents=True)
    (job / "memory.current").write_text("67108864\n", encoding="utf-8")
    if peak:
        (job / "memory.peak").write_text("134217728\n", encoding="utf-8")
    (job / "memory.events").write_text(
        "low 0\nhigh 0\nmax 1\noom 2\noom_kill 3\n", encoding="utf-8"
    )
    membership = tmp_path / "self-cgroup"
    membership.write_text("0::/user.slice/job.scope\n", encoding="utf-8")
    return mount, membership


def test_an_unreadable_membership_reports_its_own_reason(tmp_path: Path) -> None:
    """A read failure is evidence loss, not an absent cgroup v2 hierarchy."""
    from rust_prover_tools.supervision.attempt_cgroup import start_cgroup

    missing = tmp_path / "missing-membership"
    reader = CgroupV2Reader(self_cgroup=missing)

    membership = reader.discover()
    observation = start_cgroup(reader)

    assert membership.path is None
    assert membership.unavailable_reason is not None
    assert "unreadable" in membership.unavailable_reason
    assert observation.path is None
    assert observation.unavailable_reason == membership.unavailable_reason


def test_an_absent_membership_is_not_reported_as_a_read_failure(
    tmp_path: Path,
) -> None:
    """A readable file without a ``0::`` line is an ordinary absence."""
    membership = tmp_path / "self-cgroup"
    membership.write_text("1:cpu:/user.slice\n", encoding="utf-8")
    reader = CgroupV2Reader(self_cgroup=membership)

    discovered = reader.discover()

    assert discovered.path is None
    assert discovered.unavailable_reason == "no cgroup v2 membership was found"


def test_cgroup_discovery_and_memory_reads(tmp_path: Path) -> None:
    """A discovered path yields current, peak, and event counters."""
    mount, membership = _cgroup(tmp_path)
    reader = CgroupV2Reader(root=mount, self_cgroup=membership)

    membership = reader.discover()
    memory = reader.read(membership.path)

    assert membership.path == str(mount / "user.slice/job.scope")
    assert memory.current.value == 64 * 1024 * 1024
    assert memory.peak.value == 128 * 1024 * 1024
    assert memory.oom_kill.value == 3
    assert memory.oom.value == 2
    assert memory.max_events.value == 1


def test_a_missing_peak_file_is_an_explicit_absence(tmp_path: Path) -> None:
    """Older kernels do not provide ``memory.peak``."""
    mount, membership = _cgroup(tmp_path, peak=False)
    reader = CgroupV2Reader(root=mount, self_cgroup=membership)

    memory = reader.read(reader.discover().path)

    assert memory.peak.value is None
    assert memory.peak.unavailable_reason is not None
    assert memory.current.value == 64 * 1024 * 1024


def test_a_missing_cgroup_is_reported_without_raising(tmp_path: Path) -> None:
    """A host with no cgroup v2 path still produces an observation."""
    reader = CgroupV2Reader(
        root=tmp_path / "missing",
        self_cgroup=tmp_path / "missing-membership",
    )

    memory = reader.read(reader.discover().path)

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


def test_the_cgroup2_mount_is_resolved_from_mountinfo(tmp_path: Path) -> None:
    """A cgroup mounted at a non-default point and sub-tree is still found."""
    mount = tmp_path / "host" / "cgroup"
    job = mount / "leaf"
    job.mkdir(parents=True)
    (job / "memory.current").write_text("1024\n", encoding="utf-8")
    membership = tmp_path / "self-cgroup"
    membership.write_text("0::/job.scope/leaf\n", encoding="utf-8")
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text(
        (
            "24 1 8:1 / / rw - ext4 /dev/sda1 rw\n"
            "36 24 0:32 /job.scope /host/cgroup rw,nosuid - cgroup2 cgroup2 rw\n"
        ),
        encoding="utf-8",
    )
    reader = CgroupV2Reader(root=mount, self_cgroup=membership, mountinfo=mountinfo)

    membership = reader.discover()

    assert membership.path == str(job)
    assert reader.read(membership.path).current.value == 1024


def test_a_membership_outside_the_mount_root_is_unavailable(tmp_path: Path) -> None:
    """A membership the mounted superblock does not contain is not guessed."""
    membership = tmp_path / "self-cgroup"
    membership.write_text("0::/elsewhere/leaf\n", encoding="utf-8")
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text(
        "36 24 0:32 /job.scope /host/cgroup rw - cgroup2 cgroup2 rw\n",
        encoding="utf-8",
    )
    reader = CgroupV2Reader(self_cgroup=membership, mountinfo=mountinfo)

    assert reader.discover().path is None


def test_the_applicable_cgroup2_mount_is_chosen(tmp_path: Path) -> None:
    """A first mount that excludes the membership does not hide a later one."""
    second = tmp_path / "second"
    job = second / "leaf"
    job.mkdir(parents=True)
    (job / "memory.current").write_text("2048\n", encoding="utf-8")
    membership = tmp_path / "self-cgroup"
    membership.write_text("0::/job.scope/leaf\n", encoding="utf-8")
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text(
        (
            "36 24 0:32 /other /first rw - cgroup2 cgroup2 rw\n"
            "37 24 0:33 /job.scope /second rw - cgroup2 cgroup2 rw\n"
        ),
        encoding="utf-8",
    )
    reader = CgroupV2Reader(root=second, self_cgroup=membership, mountinfo=mountinfo)

    membership = reader.discover()

    assert membership.path == str(job)
    assert reader.read(membership.path).current.value == 2048


def test_readable_mountinfo_without_cgroup2_is_unavailable(tmp_path: Path) -> None:
    """A readable table with no cgroup2 entry does not use the fallback."""
    membership = tmp_path / "self-cgroup"
    membership.write_text("0::/job.scope\n", encoding="utf-8")
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text("24 1 8:1 / / rw - ext4 /dev/sda1 rw\n", encoding="utf-8")
    reader = CgroupV2Reader(self_cgroup=membership, mountinfo=mountinfo)

    assert reader.discover().path is None


def test_unreadable_mountinfo_uses_the_documented_fallback(tmp_path: Path) -> None:
    """Only an unreadable table falls back to the conventional mount."""
    membership = tmp_path / "self-cgroup"
    membership.write_text("0::/job.scope\n", encoding="utf-8")
    reader = CgroupV2Reader(
        self_cgroup=membership,
        mountinfo=tmp_path / "missing-mountinfo",
        root=tmp_path / "fallback",
    )

    assert reader.discover().path == str(tmp_path / "fallback" / "job.scope")


@pytest.mark.parametrize(
    ("raw", "decoded"),
    [
        (r"/mnt/a\040b", "/mnt/a b"),
        (r"/mnt/cg\134name", "/mnt/cg\\name"),
        (r"/mnt/cg\012x", "/mnt/cg\nx"),
        ("/plain", "/plain"),
        (r"/x\134040y", "/x\\040y"),
    ],
)
def test_mountinfo_octal_escapes_are_decoded_once(
    raw: str,
    decoded: str,
) -> None:
    """Space, tab, newline and backslash escapes all decode, exactly once."""
    from rust_prover_tools.supervision.cgroup import _unescape

    assert _unescape(raw) == decoded


def test_an_escaped_cgroup2_mount_point_is_resolved(tmp_path: Path) -> None:
    """A mount point containing an escaped space is found by its real name."""
    mount = tmp_path / "host" / "cg name"
    job = mount / "leaf"
    job.mkdir(parents=True)
    (job / "memory.current").write_text("2048\n", encoding="utf-8")
    membership = tmp_path / "self-cgroup"
    membership.write_text("0::/job.scope/leaf\n", encoding="utf-8")
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text(
        "36 24 0:32 /job.scope /host/cg\\040name rw - cgroup2 cgroup2 rw\n",
        encoding="utf-8",
    )
    reader = CgroupV2Reader(root=mount, self_cgroup=membership, mountinfo=mountinfo)

    membership = reader.discover()

    assert membership.path == str(job)


def test_an_escaped_mount_point_resolves_without_a_root_override(
    tmp_path: Path,
) -> None:
    """The decoded mount point is the base when the caller supplies no root.

    Discovery only concatenates the membership under the mount table's own
    point, so this exercises the escaped spelling without touching the real
    hierarchy and without a test override.
    """
    membership = tmp_path / "self-cgroup"
    membership.write_text("0::/job.scope/leaf\n", encoding="utf-8")
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text(
        "36 24 0:32 /job.scope /host/cg\\040name rw - cgroup2 cgroup2 rw\n",
        encoding="utf-8",
    )
    reader = CgroupV2Reader(self_cgroup=membership, mountinfo=mountinfo)

    assert reader.discover().path == "/host/cg name/leaf"


def test_an_escaped_mount_root_matching_the_membership_is_resolved(
    tmp_path: Path,
) -> None:
    """An escaped space in the mount's own root still matches the membership.

    The `root` test override replaces only the mount point. Membership
    matching still reads the escaped root field, so a space there must decode
    or a real sub-tree mount is reported as unavailable.
    """
    base = tmp_path / "override"
    job = base / "leaf"
    job.mkdir(parents=True)
    (job / "memory.current").write_text("4096\n", encoding="utf-8")
    membership = tmp_path / "self-cgroup"
    membership.write_text("0::/a b/job.scope/leaf\n", encoding="utf-8")
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text(
        "36 24 0:32 /a\\040b/job.scope /host/cgroup rw - cgroup2 cgroup2 rw\n",
        encoding="utf-8",
    )
    reader = CgroupV2Reader(root=base, self_cgroup=membership, mountinfo=mountinfo)

    discovered = reader.discover()

    assert discovered.path == str(job)
    assert reader.read(discovered.path).current.value == 4096
