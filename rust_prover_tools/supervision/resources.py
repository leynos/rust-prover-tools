"""Read Linux process-group and host memory evidence from procfs and cgroupfs.

Every read here is best-effort and returns an explained absence rather than
raising. A proof must never fail because a cgroup file is unreadable, and a
measurement that could not be taken must never be rendered as a zero: the
report distinguishes "measured 0 bytes" from "cannot measure".

What the values mean:

- Group RSS sums the resident set size of every process whose process-group ID
  matches the invocation's. Processes that leave the group with ``setsid`` or
  ``setpgid`` are outside it, as are processes that predate the spawn. Shared
  pages are counted once per process that maps them, so the sum is an upper
  bound on the group's unique resident memory, not an exact figure.
- The samples bracket the whole invocation, so they include compilation and
  any other child the verifier started.
- The cgroup values belong to the cgroup the *supervisor* is in, which on a
  hosted runner is usually shared with the rest of the job. They are therefore
  job-scoped evidence, not an exclusive per-harness measurement.
"""

from __future__ import annotations

import dataclasses as dc
import os
import sys
import typing as typ
from pathlib import Path

from rust_prover_tools.supervision.models import (
    Measurement,
    ResourceSample,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import datetime as dt

    from rust_prover_tools.supervision.models import HarnessSpec
    from rust_prover_tools.supervision.protocols import Clock

from rust_prover_tools.supervision.cgroup import CgroupV2Reader

__all__ = (
    "GROUP_ID_UNREADABLE",
    "CgroupV2Reader",
    "ProcfsResourceProbe",
    "ensure_linux",
    "read_meminfo",
    "sum_group_rss",
)

#: Reported when a process's ``stat`` file cannot be read while summing.
GROUP_ID_UNREADABLE = "process group id could not be read"

_KIB = 1024
#: Minimum ``/proc/meminfo`` fields: a value and its ``kB`` unit.
_MEMINFO_PARTS = 2
#: One past the last field this module needs from ``/proc/<pid>/stat``.
_STAT_FIELDS = 24
#: Index of the process-group field in the fields after the command name.
_PGRP_INDEX = 2
#: Index of the resident-set-size field in the fields after the command name.
_RSS_PAGES_INDEX = 21


def ensure_linux() -> None:
    """Refuse a platform whose process and memory evidence is not implemented.

    Raises
    ------
    NotImplementedError
        Raised on any platform other than Linux. The supervision layer reads
        procfs and cgroup v2, both Linux interfaces; pretending to measure
        elsewhere would produce evidence a reader could not trust.
    """
    if sys.platform != "linux":
        msg = (
            "supervised harness execution measures Linux procfs and cgroup v2 "
            f"only, not {sys.platform!r}"
        )
        raise NotImplementedError(msg)


def read_meminfo(path: Path | None = None) -> dict[str, int]:
    """Read ``/proc/meminfo`` into byte values.

    Parameters
    ----------
    path : Path | None
        Override for the meminfo file; defaults to ``/proc/meminfo``.

    Returns
    -------
    dict[str, int]
        Byte values keyed by field name without its trailing colon, such as
        ``MemAvailable``.

    Raises
    ------
    OSError
        Raised when the file cannot be read.
    """
    source = path if path is not None else _proc_meminfo()
    values: dict[str, int] = {}
    for line in source.read_text(encoding="utf-8", errors="replace").splitlines():
        name, _, rest = line.partition(":")
        parts = rest.split()
        if not parts:
            continue
        if len(parts) < _MEMINFO_PARTS or parts[1] != "kB":
            continue
        try:
            amount = int(parts[0])
        except ValueError:
            continue
        values[name] = amount * _KIB
    return values


def _proc_meminfo() -> Path:
    """Return the production ``/proc/meminfo`` path."""
    return Path("/proc/meminfo")


def _stat_field(rest: str, index: int) -> str | None:
    """Return one whitespace-separated field after a process's command name."""
    fields = rest.split()
    if index >= len(fields):
        return None
    return fields[index]


def _read_stat(pid: str, proc: Path) -> tuple[int, int] | None:
    """Return ``(pgrp, rss_pages)`` for one process, or `None` when unreadable."""
    try:
        stat = (proc / pid / "stat").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    # Everything after the last `)` starts with the state character, so field
    # three of the remainder is the process group and field twenty-two is RSS.
    _, _, rest = stat.rpartition(")")
    pgrp = _stat_field(rest, _PGRP_INDEX)
    rss = _stat_field(rest, _RSS_PAGES_INDEX)
    if pgrp is None or rss is None:
        return None
    try:
        return int(pgrp), int(rss)
    except ValueError:
        return None


def sum_group_rss(group_id: int, proc: Path | None = None) -> Measurement:
    """Sum the resident set size of every process in one process group.

    Parameters
    ----------
    group_id : int
        Owned process-group ID, which is the spawned child's PID.
    proc : Path | None
        Override for the procfs root; defaults to ``/proc``.

    Returns
    -------
    Measurement
        Summed bytes, or an explained absence when procfs cannot be read at
        all. A single unreadable member is skipped: a process that exits
        between listing and reading is normal, and treating it as a failed
        measurement would discard an otherwise usable sample.
    """
    root = proc if proc is not None else _proc_root()
    try:
        entries = list(root.iterdir())
    except OSError as error:
        return Measurement.unavailable(f"procfs cannot be read: {error.strerror}")
    summed = _sum_pages(entries, group_id, root)
    if summed is None:
        return Measurement.unavailable(GROUP_ID_UNREADABLE)
    pages, matched = summed
    if not matched:
        return Measurement.unavailable("no process in the group was found")
    return Measurement.available(pages * os.sysconf("SC_PAGE_SIZE"))


def _sum_pages(
    entries: cabc.Iterable[Path],
    group_id: int,
    root: Path,
) -> tuple[int, bool] | None:
    """Sum a group's resident pages, or `None` if no stat could be read."""
    total = 0
    read_any = False
    matched = False
    for entry in entries:
        pid = entry.name
        if not pid.isdigit():
            continue
        stat = _read_stat(pid, root)
        if stat is None:
            continue
        read_any = True
        if stat[0] == group_id:
            matched = True
            total += max(0, stat[1])
    return (total, matched) if read_any else None


def _proc_root() -> Path:
    """Return the production procfs root."""
    return Path("/proc")


@dc.dataclass(frozen=True, slots=True)
class ProcfsResourceProbe:
    """Production resource probe reading procfs for one owned process group.

    Attributes
    ----------
    clock : Clock
        Time source for the sample's wall-clock and elapsed fields.
    proc : Path | None
        Override for the procfs root, for tests.
    meminfo : Path | None
        Override for the meminfo file, for tests.
    """

    clock: Clock
    proc: Path | None = None
    meminfo: Path | None = None

    def __post_init__(self) -> None:
        """Refuse a platform whose process and memory interfaces do not exist."""
        ensure_linux()

    def sample(
        self,
        spec: HarnessSpec,
        *,
        group_id: int | None,
        started_at: dt.datetime,
        started_monotonic: float,
    ) -> ResourceSample:
        """Return one sample for a running harness invocation.

        Parameters
        ----------
        spec : HarnessSpec
            Identity the sample belongs to.
        group_id : int | None
            Owned process-group ID, or `None` when the launcher never reported
            a pid.
        started_at : datetime.datetime
            Wall-clock start of the invocation.
        started_monotonic : float
            Monotonic start of the invocation.

        Returns
        -------
        ResourceSample
            The measured values, with explained absences where a read failed.
        """
        now = self.clock.now()
        elapsed = max(0.0, self.clock.monotonic() - started_monotonic)
        rss = (
            Measurement.unavailable("the launcher reported no child process id")
            if group_id is None
            else sum_group_rss(group_id, self.proc)
        )
        host = self._host()
        return ResourceSample(
            timestamp_utc=now,
            elapsed_seconds=elapsed,
            package=spec.package,
            harness=spec.harness,
            attempt=spec.attempt,
            group_rss=rss,
            host_available=host.get("MemAvailable", Measurement.unavailable("absent")),
            host_total=host.get("MemTotal", Measurement.unavailable("absent")),
        )

    def _host(self) -> dict[str, Measurement]:
        """Read host memory values, converting a failure into absences."""
        try:
            values = read_meminfo(self.meminfo)
        except OSError as error:
            reason = f"meminfo cannot be read: {error.strerror}"
            return {
                "MemAvailable": Measurement.unavailable(reason),
                "MemTotal": Measurement.unavailable(reason),
            }
        return {
            name: (
                Measurement.available(values[name])
                if name in values
                else Measurement.unavailable(f"{name} is absent from meminfo")
            )
            for name in ("MemAvailable", "MemTotal")
        }
