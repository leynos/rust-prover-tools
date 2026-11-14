"""Shared path and error handling for the prover-tools command modules."""

from __future__ import annotations

import sys
import typing as typ
from pathlib import Path

from rust_prover_tools.exit_status import print_lines
from rust_prover_tools.verus import VerusProofFailedError

if typ.TYPE_CHECKING:
    from rust_prover_tools.errors import ProverToolError

__all__ = ("error_exit", "repo_root")


def repo_root(path: Path | None) -> Path:
    """Return the requested repository root or the current working directory.

    Parameters
    ----------
    path : Path | None
        Requested root, or `None` to use the current working directory.

    Returns
    -------
    Path
        Resolved absolute repository root.
    """
    return (path or Path.cwd()).resolve()


def error_exit(exc: ProverToolError) -> int:
    """Print a user-facing error and return the appropriate exit status.

    Parameters
    ----------
    exc : ProverToolError
        Expected tool failure to report on standard error.

    Returns
    -------
    int
        `VerusProofFailedError.exit_code` for a Verus proof failure,
        otherwise `1`.
    """
    # A closed pipe loses only this diagnostic; the proof result must survive.
    print_lines((str(exc),), stream=sys.stderr)
    match exc:
        case VerusProofFailedError(exit_code=code):
            return code
        case _:
            return 1
