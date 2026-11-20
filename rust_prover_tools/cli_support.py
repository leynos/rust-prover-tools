"""Shared path and error handling for the prover-tools command modules."""

from __future__ import annotations

import sys
import typing as typ
from pathlib import Path

from rust_prover_tools.exit_status import print_lines
from rust_prover_tools.verus import VerusProofFailedError

if typ.TYPE_CHECKING:
    from rust_prover_tools.errors import ProverToolError

__all__ = ("InputError", "error_exit", "read_input_text", "repo_root")


class InputError(Exception):
    """An input file that could not be read as text."""


def read_input_text(path: Path, description: str) -> str:
    """Read one command input as strict UTF-8, naming the file that failed.

    Parameters
    ----------
    path : Path
        File to read.
    description : str
        Short noun naming the input in the error message, such as ``log``.

    Returns
    -------
    str
        The file's decoded text.

    Raises
    ------
    InputError
        If the file cannot be read or is not valid UTF-8.

    """
    try:
        return path.read_text(encoding="utf-8")
    except OSError as error:
        msg = f"cannot read {description} {path}: {error}"
        raise InputError(msg) from error
    except UnicodeDecodeError as error:
        msg = f"{description} {path} is not valid UTF-8: {error}"
        raise InputError(msg) from error


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
