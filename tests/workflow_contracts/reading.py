"""Read the parts of a parsed workflow the contracts reason about.

Every reading here refuses a shape it does not understand rather than
returning an empty answer. The contracts built on these readings are
mostly refusals, and a refusal over an empty subject set is satisfied by
any repository at all, so "the reader found nothing" must never look like
"the repository complies".
"""

from __future__ import annotations

import typing as typ

from .loading import Document, WorkflowReadingError

if typ.TYPE_CHECKING:
    import collections.abc as cabc

#: Triggers that run a workflow with a pull request's head in view.
PULL_REQUEST_TRIGGERS: typ.Final[frozenset[str]] = frozenset({
    "pull_request",
    "pull_request_target",
})


def trigger_declaration(document: Document) -> object:
    """Return the `on:` value, read under the string key or the boolean one.

    Raises
    ------
    WorkflowReadingError
        Unless exactly one of the two keys is present. GitHub merges both,
        so a reader choosing one would be blind to the other.

    """
    keys = [key for key in ("on", True) if key in document]
    if len(keys) != 1:
        message = f"a workflow must declare `on` exactly once; found {keys}"
        raise WorkflowReadingError(message)
    return document[keys[0]]


def triggers(document: Document) -> frozenset[str]:
    """Return the trigger names in the scalar, sequence or mapping form.

    Raises
    ------
    WorkflowReadingError
        If the declaration has any other shape.

    """
    declared = trigger_declaration(document)
    names = [declared] if isinstance(declared, str) else declared
    if not isinstance(names, list | dict) or not all(
        isinstance(name, str) for name in names
    ):
        message = f"unreadable trigger declaration {declared!r}"
        raise WorkflowReadingError(message)
    return frozenset(typ.cast("cabc.Iterable[str]", names))


def trigger_filters(document: Document, trigger: str) -> dict[str, object]:
    """Return one trigger's filters, empty when it declares none.

    Raises
    ------
    WorkflowReadingError
        If the trigger's value is neither empty nor a mapping.

    """
    declared = trigger_declaration(document)
    filters = declared.get(trigger) if isinstance(declared, dict) else None
    if not isinstance(filters, dict | None):
        message = f"unreadable {trigger} filters {filters!r}"
        raise WorkflowReadingError(message)
    return typ.cast("dict[str, object]", filters or {})


def jobs(document: Document) -> dict[str, dict[str, object]]:
    """Return every job, by identifier.

    Raises
    ------
    WorkflowReadingError
        If `jobs` or any job in it is not a mapping.

    """
    declared = document.get("jobs")
    if not isinstance(declared, dict) or not all(
        isinstance(job, dict) for job in declared.values()
    ):
        message = f"unreadable jobs {declared!r}"
        raise WorkflowReadingError(message)
    return typ.cast("dict[str, dict[str, object]]", declared)


def steps(job: dict[str, object]) -> list[dict[str, object]]:
    """Return one job's steps; a reusable-workflow call has none.

    Raises
    ------
    WorkflowReadingError
        If `steps` is not a list of mappings.

    """
    declared = job.get("steps", [])
    if not isinstance(declared, list) or not all(
        isinstance(step, dict) for step in declared
    ):
        message = f"unreadable steps {declared!r}"
        raise WorkflowReadingError(message)
    return typ.cast("list[dict[str, object]]", declared)


def texts(value: object) -> cabc.Iterator[str]:
    """Yield every key and scalar in a parsed document, as text.

    Keys are read as well as values: a callee's `workflow_call` secret
    declaration names the secret only as a key.

    Examples
    --------
    >>> sorted(texts({"a": ["b", {"c": 1}]}))
    ['1', 'a', 'b', 'c']

    """
    match value:
        case dict():
            for key, child in value.items():
                yield str(key)
                yield from texts(child)
        case list():
            for child in value:
                yield from texts(child)
        case None:
            return
        case _:
            yield str(value)
