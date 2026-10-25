"""Read the cuprum version this repository selects.

The pin lives in ``pyproject.toml`` and is repeated in ``uv.lock``, once as
the resolved package entry and once as the recorded requirement that produced
it. The contract test asserts all three agree, and that the interpreter
running the suite has the same version installed.

The readers live here rather than in the contract test for the reason
:mod:`tests.workflow_contracts.coverage_python_sources` gives: the contract
test states what the repository must satisfy, while the reading is a separate
concern with its own defects to report. Keeping them apart means a broken
reader fails against the reader rather than against whichever assertion it
happened to break.

Nothing here holds a version literal. The expected version is always read from
``pyproject.toml``, so a bump moves every site together or the contract fails
with a message naming the site that lagged.
"""

from __future__ import annotations

import importlib.metadata
import re
import tomllib
import typing as typ

#: The distribution name every site constrains. PEP 503 normalisation folds
#: case, ``-``, ``_``, and ``.`` together, so only a spelling that introduces a
#: separator denotes a different project; ``cuprum`` has none to fold.
CUP: str = "cuprum"

#: The leading distribution name of a requirement, before whichever specifier
#: follows it. Matching the name separately from the specifier is what lets a
#: released range -- the drift this contract exists to catch -- be reported as
#: "not an exact pin" rather than as "cuprum is not named".
_NAME = re.compile(r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)")

#: One exact pin of ``cuprum`` and nothing else. Extras, a range, or an
#: environment marker would each let two dependency paths resolve to different
#: artefacts, which is exactly the drift a single-version contract forbids.
_EXACT_PIN = re.compile(rf"^{CUP}==(?P<version>[^\s,;\[\]]+)$")


class SelectionError(AssertionError):
    """A dependency site does not name the single exact cuprum pin.

    It subclasses ``AssertionError`` because every cause is a defect in this
    repository's own configuration, and the message names the site so the fix
    is obvious.
    """


def names_cuprum(requirement: str) -> bool:
    """Return whether ``requirement`` constrains the cuprum distribution.

    Parameters
    ----------
    requirement : str
        One requirement string from a dependency site.

    Returns
    -------
    bool
        Whether the requirement's normalised distribution name is ``cuprum``.
    """
    match = _NAME.match(requirement.strip())
    if match is None:
        return False
    return re.sub(r"[-_.]+", "-", match.group("name")).lower() == CUP


def pin_from(requirements: list[str], *, site: str) -> str:
    """Return the version pinned by the one cuprum requirement in ``requirements``.

    Parameters
    ----------
    requirements : list[str]
        Requirement strings read from one dependency site.
    site : str
        Human-readable name of the site, used in failure messages.

    Returns
    -------
    str
        The version the site pins.

    Raises
    ------
    SelectionError
        If the site names cuprum zero times or more than once, or names it
        with anything other than one exact pin.
    """
    candidates = [item for item in requirements if names_cuprum(item)]
    if len(candidates) != 1:
        message = f"{site} must name cuprum exactly once, found {candidates}"
        raise SelectionError(message)
    match = _EXACT_PIN.fullmatch(candidates[0].replace(" ", ""))
    if match is None:
        message = (
            f"{site} must pin cuprum exactly as 'cuprum==<version>' with no "
            f"extras, range, or marker, found {candidates[0]!r}"
        )
        raise SelectionError(message)
    return match.group("version")


def declared_pin(pyproject_text: str) -> str:
    """Return the exact cuprum version ``pyproject.toml`` declares.

    Parameters
    ----------
    pyproject_text : str
        The text of ``pyproject.toml``, parsed here rather than by the caller.

    Returns
    -------
    str
        The version the project's own requirement pins.

    Raises
    ------
    SelectionError
        If the requirement is missing, or is not one exact pin.
    """
    document = tomllib.loads(pyproject_text)
    requirements = document["project"]["dependencies"]
    return pin_from(requirements, site="pyproject.toml [project] dependencies")


def _packages(lock_text: str, *, site: str) -> list[dict[str, object]]:
    """Return the lock's ``package`` array, refusing any other shape.

    A lockfile carries its packages as an array of tables. A one-table
    ``[package]`` header, or a missing array, means the document is not a lock
    at all, and reporting that as "no cuprum entry" would name the wrong
    defect.

    Parameters
    ----------
    lock_text : str
        The text of a uv lockfile.
    site : str
        Human-readable name of the lock, used in failure messages.

    Returns
    -------
    list[dict[str, object]]
        The lock's package entries.

    Raises
    ------
    SelectionError
        If ``package`` is absent or is not an array of tables.
    """
    packages = tomllib.loads(lock_text).get("package", [])
    if not isinstance(packages, list) or not all(
        isinstance(entry, dict) for entry in packages
    ):
        message = f"{site} must record its packages as an array of tables"
        raise SelectionError(message)
    return [typ.cast("dict[str, object]", entry) for entry in packages]


def lock_pin(lock_text: str, *, site: str) -> str:
    """Return the version ``uv.lock`` resolves cuprum to.

    Parameters
    ----------
    lock_text : str
        The text of ``uv.lock``.
    site : str
        Human-readable name of the lock, used in failure messages.

    Returns
    -------
    str
        The locked version of cuprum.

    Raises
    ------
    SelectionError
        If the lock holds no cuprum package entry, or holds more than one.
    """
    packages = _packages(lock_text, site=site)
    locked = [
        str(entry["version"])
        for entry in packages
        if entry.get("name") == CUP and "version" in entry
    ]
    if len(locked) != 1:
        message = (
            f"{site} must contain exactly one cuprum package entry, found {locked}"
        )
        raise SelectionError(message)
    return locked[0]


def lock_specifier(lock_text: str, *, site: str) -> str:
    """Return the version the lock's own recorded requirement pins.

    A lock records the resolved version *and* a copy of the requirement that
    produced it. Checking both catches the lock that was regenerated while
    ``pyproject.toml`` moved on: the resolved version is then current but the
    recorded requirement is stale, and the next update would re-resolve it.

    Parameters
    ----------
    lock_text : str
        The text of ``uv.lock``.
    site : str
        Human-readable name of the lock, used in failure messages.

    Returns
    -------
    str
        The version the lock's recorded requirement pins.

    Raises
    ------
    SelectionError
        If the project entry or its recorded requirement is missing, or the
        requirement is not a single exact pin.
    """
    packages = _packages(lock_text, site=site)
    project = [entry for entry in packages if entry.get("name") == "rust-prover-tools"]
    if len(project) != 1:
        message = f"{site} must contain exactly one rust-prover-tools package entry"
        raise SelectionError(message)
    metadata = project[0].get("metadata", {})
    requirements = (
        metadata.get("requires-dist", []) if isinstance(metadata, dict) else []
    )
    if not isinstance(requirements, list):
        message = f"{site} must record its requirements as an array of tables"
        raise SelectionError(message)
    specifiers = [
        str(entry["specifier"])
        for entry in requirements
        if isinstance(entry, dict) and entry.get("name") == CUP
    ]
    if len(specifiers) != 1:
        message = (
            f"{site} must record exactly one cuprum requirement, found {specifiers}"
        )
        raise SelectionError(message)
    return pin_from([f"{CUP}{specifiers[0]}"], site=f"{site} requirement")


def installed_version() -> str:
    """Return the cuprum version installed in the running interpreter."""
    return importlib.metadata.version(CUP)
