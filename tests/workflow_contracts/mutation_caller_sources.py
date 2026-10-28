"""Read which Python the mutation-testing caller selects for its run.

Support for ``test_workflow_contract.py``. The shared mutation workflow
chooses the interpreter from its own ``python-version`` input and, at the
pin this repository uses, defaults an omitted input to ``3.13``. setup-uv
exports that selection as ``UV_PYTHON`` for the whole job, so a caller that
omits the input runs its project on 3.13 whatever the project's
``requires-python`` says, and ``uv`` then refuses to sync the project
against that interpreter.

Only an *omitted* input takes the default: GitHub Actions passes an
explicitly empty string through instead (actions/runner#2907), so the shared
workflow's version check rejects it rather than substituting 3.13. The readers
therefore keep "absent" and "declared blank" distinct rather than folding both
into the default.

The readers are pure over supplied text, so the contract can also be asked
what a caller this repository does not contain would do.
"""

from __future__ import annotations

import typing as typ

import yaml

from tests.workflow_contracts.coverage_python_sources import (
    rejected_versions,
    requires_python,
)

#: The shared mutation workflow's default ``python-version`` at the pin in
#: ``.github/workflows/mutation-testing.yml``. Omission selects it, which is
#: why omission is a contract failure for a project whose
#: ``requires-python`` excludes it. shared-actions#591 tracks making the
#: shared workflow diagnose that mismatch itself.
SHARED_DEFAULT_PYTHON: typ.Final[str] = "3.13"


def _mapping(value: object) -> dict[str, object]:
    """Return ``value`` when it is a mapping, otherwise an empty one."""
    return typ.cast("dict[str, object]", value) if isinstance(value, dict) else {}


def python_version_input(workflow: str) -> object:
    """Return the mutation call's raw ``python-version`` input, or ``None``.

    The value is returned uncoerced. A caller that writes ``3.14`` without
    quotes hands YAML a float, and the contract has to be able to tell that
    spelling from the string it requires; coercing here would hide it. An
    explicit ``""`` likewise stays distinguishable from an absent input,
    because only absence takes the shared default.

    Parameters
    ----------
    workflow : str
        The text of the caller workflow.

    Returns
    -------
    object
        The declared value: ``None`` when the workflow, its ``jobs``, its
        ``mutation`` job, its ``with`` mapping, or the input itself is absent
        or wrongly shaped; ``""`` when the caller declares an empty string;
        otherwise the declared value as YAML read it.

    """
    document = _mapping(yaml.safe_load(workflow))
    job = _mapping(_mapping(document.get("jobs")).get("mutation"))
    return _mapping(job.get("with")).get("python-version")


def resolved_python(selection: object) -> str:
    """Return the interpreter the shared workflow runs the project on.

    Parameters
    ----------
    selection : object
        The raw ``python-version`` input, as :func:`python_version_input`
        returns it.

    Returns
    -------
    str
        The shared workflow's own default when the caller omitted the input,
        otherwise the declared value as text. The text form is what GitHub
        Actions passes on to the input, so a bare YAML float still names a
        version the project's range can be compared against, and an explicit
        empty string stays empty rather than becoming the default.

    """
    # Only an absent input takes the default. A declared value is either a
    # string or a number (an unquoted YAML float); both are passed on as text.
    if selection is None:
        return SHARED_DEFAULT_PYTHON
    return str(selection)


def is_explicit_and_accepted(workflow: str, pyproject: str) -> bool:
    """Say whether a caller selects an explicit string the project accepts.

    Both readers are the ones the repository contract applies, so this answers
    for the checked-in caller and for a synthetic one alike, and the synthetic
    cases cannot drift from the real check.

    Parameters
    ----------
    workflow : str
        The text of the caller workflow.
    pyproject : str
        The text of the project declaration holding ``requires-python``.

    Returns
    -------
    bool
        ``True`` only when the caller declares the input, declares it as a
        non-empty string, and names a version inside ``requires-python``. An
        omitted input resolves to the shared default, an unquoted float is not
        a string, and an explicit empty string names no version; all fail.

    """
    selection = python_version_input(workflow)
    is_explicit = isinstance(selection, str) and bool(selection)
    # An empty resolved value is already refused above, so the range check
    # only ever sees a version worth parsing.
    return is_explicit and not rejected_versions(
        requires_python(pyproject), [resolved_python(selection)]
    )
