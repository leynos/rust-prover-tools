"""Load fenced examples from the documentation that ships with the project.

The documentation style guide requires every user-facing example to carry a
stable identifier in a marker comment directly above its fence. Tests then read
the published document instead of a copied fixture, so the example they
exercise is the text a reader sees and cannot drift away from it.

This module is the single reader of that convention. It parses a document into
examples keyed by identifier and refuses a document a reader could not trust: a
marker with no fence beneath it, a marker carrying no identifier, a fence no
marker introduces, an unterminated fence, or two examples that claim the same
identifier. A skipped fence would be documentation no test could ever hold, so
it is an error rather than something to walk past.
"""

from __future__ import annotations

import dataclasses as dc
import re

#: A marker line carrying the identifier of the fence beneath it.
_MARKER = re.compile(
    r"^\s*<!--\s*tested-example:\s*(?P<identifier>\S(?:.*\S)?)\s*-->\s*$"
)

#: Any line that means to be a marker, well formed or not. It is checked after
#: `_MARKER` so a marker that carries no identifier is refused rather than
#: skipped: silently ignoring it would leave its fence unregistered.
_MARKER_INTENT = re.compile(r"^\s*<!--\s*tested-example\b")

#: The opening fence of an example block.
_FENCE = re.compile(
    r"^(?P<indent>[ \t]*)(?P<fence>`{3,})(?P<language>[A-Za-z0-9_+-]*)\s*$"
)


class DocumentedExampleError(ValueError):
    """A document does not follow the tested-example convention."""


@dc.dataclass(frozen=True, slots=True)
class DocumentedExample:
    """One identified, fenced example read from a document.

    Attributes
    ----------
    identifier : str
        The stable identifier the marker above the fence carries.
    language : str
        The fence's language tag, or the empty string when it has none.
    body : str
        The example's lines, without the surrounding fences.
    """

    identifier: str
    language: str
    body: str


def _closing_fence(line: str, fence: str) -> bool:
    """Say whether `line` closes a fence opened by `fence`."""
    stripped = line.strip()
    return (
        stripped.startswith(fence)
        and set(stripped) == {"`"}
        and len(stripped) >= len(fence)
    )


def _read_body(
    lines: list[str],
    start: int,
    fence: str,
    *,
    label: tuple[str, str],
) -> tuple[DocumentedExample, int]:
    """Read one example's body and return it with the next line to inspect.

    `label` carries the identifier and the document name together, so the
    reader needs one argument rather than two for the same diagnostic.
    """
    identifier, source = label
    body: list[str] = []
    index = start
    while index < len(lines):
        if _closing_fence(lines[index], fence):
            opening = _FENCE.match(lines[start - 1])
            language = opening.group("language") if opening else ""
            return (
                DocumentedExample(
                    identifier=identifier, language=language, body="\n".join(body)
                ),
                index + 1,
            )
        body.append(lines[index])
        index += 1
    msg = (
        f"{source}: example {identifier!r} starting at line {start} "
        "has no closing fence"
    )
    raise DocumentedExampleError(msg)


def load_examples(
    text: str, *, source: str = "document"
) -> dict[str, DocumentedExample]:
    """Return every marked example in `text`, keyed by its identifier.

    Parameters
    ----------
    text : str
        The document to parse, normally read from the shipped file.
    source : str
        A name for the document used in error messages.

    Returns
    -------
    dict[str, DocumentedExample]
        The examples in document order, keyed by identifier.

    Raises
    ------
    DocumentedExampleError
        If a marker carries no identifier, a marker is not immediately followed
        by a fence, a fence has no marker above it, a fence is not closed, or an
        identifier appears more than once.
    """
    lines = text.splitlines()
    examples: dict[str, DocumentedExample] = {}
    index = 0
    while index < len(lines):
        marker = _MARKER.match(lines[index])
        if marker is not None:
            identifier = marker.group("identifier")
            if identifier in examples:
                msg = f"{source}: example identifier {identifier!r} is used twice"
                raise DocumentedExampleError(msg)
            opening = _FENCE.match(lines[index + 1]) if index + 1 < len(lines) else None
            if opening is None:
                msg = (
                    f"{source}: marker for {identifier!r} at line {index + 1} "
                    "is not immediately followed by a fence"
                )
                raise DocumentedExampleError(msg)
            example, index = _read_body(
                lines,
                index + 2,
                opening.group("fence"),
                label=(identifier, source),
            )
            examples[identifier] = example
            continue
        if _MARKER_INTENT.match(lines[index]) is not None:
            msg = (
                f"{source}: marker at line {index + 1} carries no identifier, "
                "so its example cannot be registered"
            )
            raise DocumentedExampleError(msg)
        if _FENCE.match(lines[index]) is not None:
            msg = (
                f"{source}: fence at line {index + 1} has no "
                "`<!-- tested-example: IDENTIFIER -->` marker above it"
            )
            raise DocumentedExampleError(msg)
        index += 1
    return examples
