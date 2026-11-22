"""Read cgroup v2 membership and memory evidence without raising.

The values belong to the cgroup the supervisor is in, which on a hosted runner
is normally shared with the rest of the job. They are job-scoped evidence, not
an exclusive per-harness measurement.
"""

from __future__ import annotations

import dataclasses as dc
from pathlib import Path

from rust_prover_tools.supervision.models import CgroupMemory, Measurement

__all__ = ("CgroupV2Reader",)


#: Production cgroup v2 mount point.
_CGROUP_ROOT = "/sys/fs/cgroup"
#: The supervisor's own cgroup membership line.
_CGROUP_SELF = "/proc/self/cgroup"


def _read_text(path: Path) -> str | None:
    """Read one cgroup file, returning `None` rather than raising."""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _read_int(path: Path, *, key: str | None = None) -> Measurement:
    """Read an integer from one cgroup file, or an explained absence."""
    text = _read_text(path)
    if text is None:
        return Measurement.unavailable(f"{path.name} is unreadable")
    if key is not None:
        for line in text.splitlines():
            name, _, rest = line.partition(" ")
            if name == key and rest.strip().isdigit():
                return Measurement.available(int(rest.strip()))
        return Measurement.unavailable(f"{key} is absent from {path.name}")
    stripped = text.strip()
    return (
        Measurement.available(int(stripped))
        if stripped.isdigit()
        else Measurement.unavailable(f"{path.name} is not an integer")
    )


@dc.dataclass(frozen=True, slots=True)
class CgroupV2Reader:
    """Discover the cgroup v2 path and read its memory files.

    Attributes
    ----------
    root : Path | None
        Override for the cgroup v2 mount point, for tests.
    self_cgroup : Path | None
        Override for ``/proc/self/cgroup``, for tests.
    """

    root: Path | None = None
    self_cgroup: Path | None = None

    def discover(self) -> str | None:
        """Return the applicable cgroup v2 path, or `None` when none exists.

        Returns
        -------
        str | None
            The ``0::`` membership path from ``/proc/self/cgroup`` resolved
            under the cgroup v2 mount, or `None` when the host does not expose
            one.
        """
        source = self.self_cgroup if self.self_cgroup is not None else _self_path()
        text = _read_text(source)
        if text is None:
            return None
        for line in text.splitlines():
            hierarchy, _, path = line.partition(":")
            _, _, mount = path.partition(":")
            if hierarchy == "0" and mount.startswith("/"):
                return mount
        return None

    def read(self, path: str | None) -> CgroupMemory:
        """Read one instant of the cgroup's memory files.

        Parameters
        ----------
        path : str | None
            Membership path from `discover`.

        Returns
        -------
        CgroupMemory
            The current, peak, and ``oom_kill`` values. An absent path yields
            values that all carry the same explained absence, so a report
            never renders the absence as zero.
        """
        if path is None:
            reason = "no cgroup v2 path was discovered"
            return CgroupMemory(
                current=Measurement.unavailable(reason),
                peak=Measurement.unavailable(reason),
                oom_kill=Measurement.unavailable(reason),
            )
        base = (self.root if self.root is not None else _cgroup_root()) / path.lstrip(
            "/"
        )
        return CgroupMemory(
            current=_read_int(base / "memory.current"),
            peak=_read_int(base / "memory.peak"),
            oom_kill=_read_int(base / "memory.events", key="oom_kill"),
        )


def _self_path() -> Path:
    """Return the production ``/proc/self/cgroup`` path."""
    return Path(_CGROUP_SELF)


def _cgroup_root() -> Path:
    """Return the production cgroup v2 mount point."""
    return Path(_CGROUP_ROOT)
