"""Parse GitHub Actions workflows strictly enough for a contract to trust.

Every reading here refuses a shape it does not understand rather than
returning an empty answer. The contracts built on these readings are
mostly refusals, and a refusal over an empty subject set is satisfied by
any repository at all, so "the reader found nothing" must never look like
"the repository complies".
"""

from __future__ import annotations

import typing as typ

import yaml
from yaml.constructor import ConstructorError

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

#: A parsed workflow. The key type is `object` because YAML 1.1 resolves
#: an unquoted `on:` to the boolean `True`, so a real workflow's trigger
#: key is not a string at all.
type Document = dict[object, object]

#: Triggers that run a workflow with a pull request's head in view.
PULL_REQUEST_TRIGGERS: typ.Final[frozenset[str]] = frozenset({
    "pull_request",
    "pull_request_target",
})

#: File suffixes GitHub runs as workflows, compared without case.
WORKFLOW_SUFFIXES: typ.Final[frozenset[str]] = frozenset({".yml", ".yaml"})


class WorkflowReadingError(Exception):
    """Raised when a workflow cannot be read into a shape the rules trust."""


class _UniqueKeyLoader(yaml.SafeLoader):
    """A `yaml.SafeLoader` refusing a mapping that declares a key twice.

    PyYAML keeps the last of two equal keys and says nothing, so a job
    declaring `runs-on` twice parses into a document holding only the
    second value while the rules read the half GitHub may not run.
    """

    @typ.override
    def construct_mapping(
        self, node: yaml.MappingNode, deep: bool = False
    ) -> dict[typ.Hashable, typ.Any]:
        """Construct one mapping, refusing a key already seen in it.

        Raises
        ------
        ConstructorError
            If a key appears twice, naming it and where it appears.
        """
        seen: set[object] = set()
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=deep)
            if key in seen:
                context = "while constructing a mapping"
                problem = f"found duplicate key {key!r}"
                raise ConstructorError(
                    context, node.start_mark, problem, key_node.start_mark
                )
            seen.add(key)
        return super().construct_mapping(node, deep=deep)


def load_workflow(text: str) -> Document:
    r"""Parse one workflow, refusing duplicate keys and non-mapping documents.

    Raises
    ------
    WorkflowReadingError
        If the text is not YAML, repeats a key, or is not a mapping.

    Examples
    --------
    >>> load_workflow("on: push\njobs: {}\n")
    {True: 'push', 'jobs': {}}
    """
    try:
        # A SafeLoader subclass constructs only plain data, never objects.
        parsed = yaml.load(text, Loader=_UniqueKeyLoader)  # ruff: ignore[unsafe-yaml-load]
    except yaml.YAMLError as error:
        message = f"not a workflow document: {error}"
        raise WorkflowReadingError(message) from error
    if not isinstance(parsed, dict):
        message = "a workflow must parse to a top-level mapping"
        raise WorkflowReadingError(message)
    return typ.cast("Document", parsed)


def read_workflows(directory: Path) -> dict[str, Document]:
    """Return every workflow under one directory, by file name.

    Both suffixes are read, whatever their case, because GitHub runs a
    workflow named either way.

    Raises
    ------
    WorkflowReadingError
        If the directory holds no workflow, or one does not parse; the
        message names the file.
    """
    paths = sorted(
        path
        for path in directory.iterdir()
        if path.suffix.casefold() in WORKFLOW_SUFFIXES
    )
    if not paths:
        message = f"no workflow was read from {directory}; the reader is broken"
        raise WorkflowReadingError(message)
    return {path.name: _load_file(path) for path in paths}


def _load_file(path: Path) -> Document:
    """Parse one workflow file, naming it in any failure."""
    try:
        return load_workflow(path.read_text(encoding="utf-8"))
    except WorkflowReadingError as error:
        message = f"{path.name}: {error}"
        raise WorkflowReadingError(message) from error


def trigger_declaration(document: Document) -> object:
    """Return the `on:` value, read under the string key or the boolean one.

    Raises
    ------
    WorkflowReadingError
        If both keys are present, since GitHub merges them and a reader
        choosing one is blind to the other, or if neither is.
    """
    if "on" in document and True in document:
        message = "the workflow declares its triggers under both `on` and `true`"
        raise WorkflowReadingError(message)
    if "on" in document:
        return document["on"]
    if True in document:
        return document[True]
    message = "the workflow declares no triggers"
    raise WorkflowReadingError(message)


def triggers(document: Document) -> frozenset[str]:
    """Return the trigger names in the scalar, sequence or mapping form.

    Raises
    ------
    WorkflowReadingError
        If the declaration has any other shape.
    """
    declared = trigger_declaration(document)
    match declared:
        case str():
            return frozenset({declared})
        case list() if all(isinstance(name, str) for name in declared):
            return frozenset(typ.cast("list[str]", declared))
        case dict() if all(isinstance(name, str) for name in declared):
            return frozenset(typ.cast("dict[str, object]", declared))
        case _:
            message = f"unreadable trigger declaration {declared!r}"
            raise WorkflowReadingError(message)


def trigger_filters(document: Document, trigger: str) -> dict[str, object]:
    """Return one trigger's filters, empty when it declares none.

    Raises
    ------
    WorkflowReadingError
        If the trigger's value is neither empty nor a mapping.
    """
    declared = trigger_declaration(document)
    if not isinstance(declared, dict):
        return {}
    filters = declared.get(trigger)
    if filters is None:
        return {}
    if not isinstance(filters, dict):
        message = f"unreadable {trigger} filters {filters!r}"
        raise WorkflowReadingError(message)
    return typ.cast("dict[str, object]", filters)


def jobs(document: Document) -> dict[str, dict[str, object]]:
    """Return every job, by identifier.

    Raises
    ------
    WorkflowReadingError
        If `jobs` or any job in it is not a mapping.
    """
    declared = document.get("jobs")
    if not isinstance(declared, dict):
        message = "the workflow's jobs are not a mapping"
        raise WorkflowReadingError(message)
    for name, job in declared.items():
        if not isinstance(job, dict):
            message = f"job {name!r} is not a mapping"
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
