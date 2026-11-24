"""Capture one child's pid, settled status, and owned parent descriptors.

The launcher's recorder is a Cuprum observer: the ``start`` event carries the
pid only for a hook registered in the same context as the run, and the parent's
copies of the child's pipe ends must close immediately after the spawn or the
read ends never see EOF.
"""

from __future__ import annotations

import contextlib
import os
import typing as typ

if typ.TYPE_CHECKING:
    from cuprum.events import ExecEvent

__all__ = ("PidRecorder",)


class PidRecorder:
    """Capture the child PID and settled status from the run's events."""

    def __init__(self) -> None:
        """Create a recorder that has seen no event."""
        self.pid: int | None = None
        self.settled_exit_code: int | None = None
        self._closing: list[int] = []

    def own(self, *descriptors: int) -> None:
        """Close the parent's copies of the child's pipe ends after the spawn.

        The write ends must leave the parent once the child owns them, or the
        read ends never observe EOF.
        """
        self._closing.extend(descriptors)

    def __call__(self, event: ExecEvent) -> None:
        """Record the pid and any status the run announced."""
        if event.phase == "start":
            if event.pid is not None:
                self.pid = event.pid
            self.close_parent_ends()
        if event.phase in {"exit", "settled"} and event.exit_code is not None:
            self.settled_exit_code = event.exit_code

    def close_parent_ends(self) -> None:
        """Close every borrowed descriptor once, tolerating a repeated call."""
        for descriptor in self._closing:
            with contextlib.suppress(OSError):
                os.close(descriptor)
        self._closing.clear()
