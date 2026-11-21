"""Select and qualify the Z3 executable a Verus run is bound to.

Verus does not bundle the solver it was tested with: it runs whichever ``z3``
``VERUS_Z3_PATH`` names, or one it finds itself.  Two runs that differ only in
the solver can therefore disagree, which makes the solver part of the claim a
verification makes.  This module resolves a selection to one executable and
holds it to the release this package pins.

Selection and qualification are separate steps on purpose.  Resolving a name
reads the filesystem; observing a version launches the executable and belongs
to the command boundary.  :func:`qualify_z3` therefore takes the exit status
and output it is given, so a caller cannot mistake it for a step that runs
anything.
"""

from __future__ import annotations

import dataclasses as dc
import shutil
import typing as typ
from pathlib import Path

from rust_prover_tools.binary_assets import Z3_VERSION
from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.z3 import default_z3_install_dir

__all__ = (
    "CALIBRATED_Z3_BANNER",
    "EXPLICIT_SOURCE",
    "PINNED_SOURCE",
    "PINNED_Z3",
    "Z3Selection",
    "qualify_z3",
    "resolve_z3",
)

#: The banner the pinned release prints for ``z3 --version``.
CALIBRATED_Z3_BANNER: typ.Final[str] = f"Z3 version {Z3_VERSION} - 64 bit"

#: The selection name that resolves the release this package installs.
PINNED_Z3: typ.Final[str] = "pinned"
PINNED_SOURCE: typ.Final[str] = "pinned"
EXPLICIT_SOURCE: typ.Final[str] = "explicit"


@dc.dataclass(frozen=True, slots=True)
class Z3Selection:
    """One qualified solver: where it is, what it reported, where it came from.

    Attributes
    ----------
    executable : Path
        Absolute path of the qualified solver.
    banner : str
        The exact text ``z3 --version`` printed.
    source : str
        ``pinned`` when the package's own installation supplied the solver,
        ``explicit`` when a caller named one.
    """

    executable: Path
    banner: str
    source: str


def resolve_z3(
    selection: str,
    *,
    repo_root: Path,
    install_dir: Path | None = None,
) -> Path:
    """Resolve a solver selection to an executable without running it.

    Parameters
    ----------
    selection : str
        ``pinned``, an executable path, or a command name to look up on
        ``PATH``.
    repo_root : Path
        Repository root used to find the pinned installation.
    install_dir : Path | None
        Directory holding the pinned ``z3``, overriding the repository default.

    Returns
    -------
    Path
        The selected executable.

    Raises
    ------
    ProverToolError
        If the pinned installation is missing, or an explicit selection names
        no executable.
    """
    if selection == PINNED_Z3:
        directory = install_dir or default_z3_install_dir(repo_root)
        # Resolve before probing.  A relative install directory would
        # otherwise return a bare ``z3`` that the probe resolves through
        # PATH, qualifying one binary and later binding another.
        executable = (directory / "z3").resolve()
        if not executable.is_file():
            msg = (
                f"the pinned Z3 {Z3_VERSION} is not installed at {executable}; "
                "run `prover-tools z3 install` first"
            )
            raise ProverToolError(msg)
        return executable
    candidate = Path(selection)
    if _names_a_path(selection) and candidate.is_file():
        return candidate.resolve()
    found = shutil.which(selection)
    if found is None:
        msg = f"the Z3 solver {selection!r} names no executable"
        raise ProverToolError(msg)
    return Path(found).resolve()


def _names_a_path(selection: str) -> bool:
    """Report whether a selection names a filesystem path rather than a command.

    A bare command name resolves through ``PATH``, as the shell would resolve
    it.  A same-named file in the working directory is not what the caller
    asked for, and honouring it would bind the run to a solver the selection
    never named.  Anything carrying a separator, and the spellings ``.`` and
    ``..``, is a path and is read from the filesystem.
    """
    return Path(selection).name != selection or selection in {".", ".."}


def qualify_z3(
    executable: Path,
    *,
    exit_code: int,
    reported: str,
    source: str,
) -> Z3Selection:
    """Hold an observed solver version to the release this package pins.

    Parameters
    ----------
    executable : Path
        The executable whose version was observed.
    exit_code : int
        Exit status of ``executable --version``.
    reported : str
        Standard output of that invocation.
    source : str
        ``pinned`` or ``explicit``, recorded beside the selection.

    Returns
    -------
    Z3Selection
        The qualified solver, with the banner exactly as it was reported.

    Raises
    ------
    ProverToolError
        If the probe failed or reported a release other than the calibrated
        one.  The message names the banner observed, so a mismatched pin is
        distinguishable from an unrunnable binary.
    """
    banner = reported.strip()
    if exit_code == 0 and banner == CALIBRATED_Z3_BANNER:
        return Z3Selection(executable.resolve(), banner, source)
    observed = banner or "no version"
    msg = (
        f"the solver {executable} must report {CALIBRATED_Z3_BANNER!r}; "
        f"it reported {observed!r}"
    )
    raise ProverToolError(msg)
