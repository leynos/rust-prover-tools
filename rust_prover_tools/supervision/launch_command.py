"""The composed invocation one harness launcher will run.

Kept apart from the launcher so command composition can be read, and imported,
without the process machinery around it.
"""

from __future__ import annotations

import dataclasses as dc
import typing as typ

from cuprum.context import EnvMode

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

    from cuprum.context import UnsetType

    from rust_prover_tools.supervision.models import HarnessSpec

__all__ = ("CommandBuilder", "LaunchCommand")


@dc.dataclass(frozen=True, slots=True)
class LaunchCommand:
    """One composed invocation the launcher will run.

    Attributes
    ----------
    argv : tuple[str, ...]
        Program and arguments.
    cwd : Path | None
        Working directory for the child.
    env : cabc.Mapping[str, str | UnsetType] | None
        Environment for the child. Wrapper values and removal markers are
        retained verbatim.
    env_mode : cuprum.EnvMode
        How `env` relates to the child's inherited environment. A supervised
        legacy route uses `REPLACE` so a later host change cannot reach a
        derived command; a snapshot route keeps the overlay its removal
        markers require.
    """

    argv: tuple[str, ...]
    cwd: Path | None = None
    env: cabc.Mapping[str, str | UnsetType] | None = None
    env_mode: EnvMode = EnvMode.OVERLAY


type CommandBuilder = cabc.Callable[[HarnessSpec], LaunchCommand]
"""Composes the argv and environment for one harness identity."""

type CommandBuilder = cabc.Callable[[HarnessSpec], LaunchCommand]
"""Composes the argv and environment for one harness identity."""
