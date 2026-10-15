"""Adapt public Kani execution options to the qualified release workflow.

Literal path and verifier argument inputs become KaniExecOptions, while typed
validation failures and child signals retain their documented CLI status.
"""

import os
import sys
import typing as typ
from pathlib import Path

from cyclopts import Parameter

from rust_prover_tools.errors import CommandFailedError, ProverToolError
from rust_prover_tools.kani_release import KaniExecOptions, exec_kani


def kani_exec(
    *,
    installation: str,
    cwd: str,
    extra_arg: typ.Annotated[list[str], Parameter(allow_repeating=True)],
) -> int:
    """Execute an already installed, qualified Kani 0.67 Linux release.

    Parameters
    ----------
    installation : str
        Literal release-root path text, containing bin and toolchain directories.
    cwd : str
        Literal proof-directory path text; relative paths use the process cwd.
    extra_arg : list[str]
        Literal verifier arguments, provided with repeated --extra-arg options.

    Returns
    -------
    int
        Verifier exit status; validation errors return one.
    """
    try:
        status = exec_kani(
            KaniExecOptions(Path(installation), Path(cwd), tuple(extra_arg)),
            dict(os.environ),
        )
        return 128 - status if status < 0 else status
    except CommandFailedError as exc:
        print(exc, file=sys.stderr)
        return 128 - exc.exit_code if exc.exit_code < 0 else exc.exit_code
    except ProverToolError as exc:
        print(exc, file=sys.stderr)
        return 1
