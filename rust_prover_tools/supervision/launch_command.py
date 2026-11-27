"""The composed invocation one harness launcher will run.

Kept apart from the launcher so command composition can be read, and imported,
without the process machinery around it. The environment is described in
prover-neutral terms; the translation to Cuprum's own types happens only in the
process boundary that builds the command.
"""

from __future__ import annotations

import dataclasses as dc
import typing as typ

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

    from rust_prover_tools.supervision.models import HarnessSpec

__all__ = ("CommandBuilder", "LaunchCommand", "LaunchEnvironment")


@dc.dataclass(frozen=True, slots=True)
class LaunchEnvironment:
    """A child environment in prover-neutral terms.

    Attributes
    ----------
    values : Mapping[str, str | None]
        Environment entries. A `None` value removes an inherited variable.
    replace : bool
        Whether to replace the inherited environment rather than overlay it.
    """

    values: cabc.Mapping[str, str | None]
    replace: bool = False


@dc.dataclass(frozen=True, slots=True)
class LaunchCommand:
    """One composed invocation the launcher will run.

    Attributes
    ----------
    argv : tuple[str, ...]
        Program and arguments.
    cwd : Path | None
        Working directory for the child.
    env : LaunchEnvironment | None
        Environment for the child, in prover-neutral terms. `None` inherits.
    """

    argv: tuple[str, ...]
    cwd: Path | None = None
    env: LaunchEnvironment | None = None


type CommandBuilder = cabc.Callable[[HarnessSpec], LaunchCommand]
"""Composes the argv and environment for one harness identity."""
