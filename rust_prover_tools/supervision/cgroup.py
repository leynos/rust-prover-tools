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


#: Fallback cgroup v2 mount point, used only when mountinfo is unreadable.
_CGROUP_ROOT = "/sys/fs/cgroup"
#: The supervisor's own cgroup membership line.
_CGROUP_SELF = "/proc/self/cgroup"
#: The host's mount table, used to resolve the cgroup v2 superblock.
_CGROUP_MOUNTINFO = "/proc/self/mountinfo"
#: Fields before the ``-`` separator in a mountinfo line.
_MOUNTINFO_PREFIX_FIELDS = 5


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
        Override for the directory the cgroup v2 superblock is mounted at, for
        tests. The mount's own root path still comes from mountinfo when a
        `mountinfo` override is supplied, so a sub-tree mount can be exercised
        without writing to the real hierarchy.
    self_cgroup : Path | None
        Override for ``/proc/self/cgroup``, for tests.
    mountinfo : Path | None
        Override for ``/proc/self/mountinfo``, for tests.
    """

    root: Path | None = None
    self_cgroup: Path | None = None
    mountinfo: Path | None = None

    def discover(self) -> str | None:
        """Return the applicable cgroup v2 directory, or `None` when none exists.

        The membership path from ``/proc/self/cgroup`` is relative to the
        mounted superblock's root, not to the mount point. A host may mount the
        hierarchy with a non-root sub-tree, so the mount table is consulted
        rather than assuming ``/sys/fs/cgroup``.

        Returns
        -------
        str | None
            The resolved filesystem directory, or `None` when the host exposes
            no cgroup v2 membership.
        """
        membership = self._membership()
        if membership is None:
            return None
        mount = _cgroup2_mount(
            self.mountinfo if self.mountinfo is not None else _mountinfo_path()
        )
        mount_root, mount_point = mount if mount is not None else ("/", _CGROUP_ROOT)
        if self.root is not None:
            mount_point = str(self.root)
        relative = _relative_membership(membership, mount_root)
        if relative is None:
            return None
        return str(Path(mount_point) / relative)

    def _membership(self) -> str | None:
        """Return the ``0::`` cgroup v2 membership path, when one exists."""
        source = self.self_cgroup if self.self_cgroup is not None else _self_path()
        text = _read_text(source)
        if text is None:
            return None
        for line in text.splitlines():
            hierarchy, _, path = line.partition(":")
            _, _, membership = path.partition(":")
            if hierarchy == "0" and membership.startswith("/"):
                return membership
        return None

    @staticmethod
    def read(path: str | None) -> CgroupMemory:
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
                oom=Measurement.unavailable(reason),
                max_events=Measurement.unavailable(reason),
            )
        base = Path(path)
        events = base / "memory.events"
        return CgroupMemory(
            current=_read_int(base / "memory.current"),
            peak=_read_int(base / "memory.peak"),
            oom_kill=_read_int(events, key="oom_kill"),
            oom=_read_int(events, key="oom"),
            max_events=_read_int(events, key="max"),
        )


def _self_path() -> Path:
    """Return the production ``/proc/self/cgroup`` path."""
    return Path(_CGROUP_SELF)


def _mountinfo_path() -> Path:
    """Return the production ``/proc/self/mountinfo`` path."""
    return Path(_CGROUP_MOUNTINFO)


def _cgroup2_mount(path: Path) -> tuple[str, str] | None:
    r"""Return the ``(root, mount point)`` of the cgroup2 superblock.

    A mountinfo line is ``id parent major:minor root mount-point options
    [optional...] - fstype source super-options``. Spaces inside a field are
    escaped as ``\040``.
    """
    text = _read_text(path)
    if text is None:
        return None
    for line in text.splitlines():
        before, separator, after = line.partition(" - ")
        if not separator:
            continue
        fields = before.split()
        trailing = after.split()
        if len(fields) < _MOUNTINFO_PREFIX_FIELDS or not trailing:
            continue
        if trailing[0] != "cgroup2":
            continue
        return _unescape(fields[3]), _unescape(fields[4])
    return None


def _unescape(field: str) -> str:
    """Undo the octal escapes mountinfo applies to spaces and tabs."""
    return field.replace("\\040", " ").replace("\\011", "\t")


def _relative_membership(membership: str, mount_root: str) -> str | None:
    """Return `membership` relative to a mount root, or `None` if outside it."""
    root = mount_root.rstrip("/") or "/"
    if root == "/":
        return membership.lstrip("/")
    if membership == root:
        return ""
    prefix = f"{root}/"
    if membership.startswith(prefix):
        return membership[len(prefix) :]
    return None
