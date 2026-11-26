"""Read cgroup v2 membership and memory evidence without raising.

The values belong to the cgroup the supervisor is in, which on a hosted runner
is normally shared with the rest of the job. They are job-scoped evidence, not
an exclusive per-harness measurement.
"""

from __future__ import annotations

import dataclasses as dc
from pathlib import Path

from rust_prover_tools.supervision.models import (
    CgroupMembership,
    CgroupMemory,
    Measurement,
)

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

    def discover(self) -> CgroupMembership:
        """Return the applicable cgroup v2 directory or the reason for none.

        The membership path from ``/proc/self/cgroup`` is relative to the
        mounted superblock's root, not to the mount point. A host may mount the
        hierarchy with a non-root sub-tree, so the mount table is consulted
        rather than assuming ``/sys/fs/cgroup``. A membership file that cannot
        be read reports its own reason, so a read failure is never mistaken for
        a host without cgroup v2.

        Returns
        -------
        CgroupMembership
            The resolved filesystem directory, or the reason none was found.
        """
        membership = self._membership()
        if membership is None:
            return self._absence()
        mounts = _cgroup2_mounts(
            self.mountinfo if self.mountinfo is not None else _mountinfo_path()
        )
        if mounts is None:
            # Mountinfo itself is unreadable. The documented fallback is the
            # conventional mount point, used only here.
            base = self.root if self.root is not None else Path(_CGROUP_ROOT)
            return CgroupMembership(path=str(base / membership.lstrip("/")))
        # Several cgroup2 superblocks can be mounted. The applicable one is
        # the mount whose own root contains the membership; the longest
        # matching root wins when mounts nest.
        for mount_root, mount_point in sorted(
            mounts, key=lambda mount: len(mount[0]), reverse=True
        ):
            relative = _relative_membership(membership, mount_root)
            if relative is None:
                continue
            base = self.root if self.root is not None else Path(mount_point)
            return CgroupMembership(path=str(base / relative))
        return CgroupMembership.absent()

    def _absence(self) -> CgroupMembership:
        """Report why no membership was resolved, keeping a read failure apart."""
        source = self.self_cgroup if self.self_cgroup is not None else _self_path()
        if _read_text(source) is None:
            return CgroupMembership.unreadable(
                f"{source.name} is unreadable, so cgroup v2 membership is unknown"
            )
        return CgroupMembership.absent()

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


def _cgroup2_mounts(path: Path) -> list[tuple[str, str]] | None:
    r"""Return every cgroup2 superblock's ``(root, mount point)``.

    A mountinfo line is ``id parent major:minor root mount-point options
    [optional...] - fstype source super-options``. Spaces inside a field are
    escaped as ``\040``. `None` means mountinfo could not be read at all,
    which is different from a readable table with no cgroup2 entry.
    """
    text = _read_text(path)
    if text is None:
        return None
    mounts: list[tuple[str, str]] = []
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
        mounts.append((_unescape(fields[3]), _unescape(fields[4])))
    return mounts


_ESCAPES = {"040": " ", "011": "\t", "012": "\n", "134": "\\"}


def _unescape(field: str) -> str:
    r"""Undo every octal escape mountinfo applies to a path.

    Linux escapes space, tab, newline and backslash as ``\040``, ``\011``,
    ``\012`` and ``\134``. Decoding runs once, left to right, so a literal
    ``\134`` that spells an escape is not decoded twice.
    """
    pieces: list[str] = []
    index = 0
    while index < len(field):
        if (
            field[index] == "\\"
            and (escape := field[index + 1 : index + 4]) in _ESCAPES
        ):
            pieces.append(_ESCAPES[escape])
            index += 4
        else:
            pieces.append(field[index])
            index += 1
    return "".join(pieces)


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
