"""Parse one module level of masked Rust into proofs and module declarations.

The parser recognises only items harness discovery needs: ``mod`` declarations,
inline modules, and functions selected by a Kani proof attribute. Every other
item is skipped as a balanced unit, so a struct body, an ``impl`` block, or a
``macro_rules!`` body cannot leak its contents into the module level.

``cfg`` attributes are recorded, never evaluated. ``cfg(kani)`` is the normal
Kani activation and is not conditional; any other ``cfg`` marks the item and
everything nested inside it as conditional.
"""

from __future__ import annotations

import dataclasses as dc

from rust_prover_tools.kani_inventory.attribute_values import (
    DEPTH_DELTAS,
    PROOF_ATTRIBUTES,
    attribute_name,
    cfg_attr_facts,
    normalise,
    path_value,
)
from rust_prover_tools.kani_inventory.masking import Identifier, identifier
from rust_prover_tools.kani_inventory.models import InventoryDiscoveryError

__all__ = (
    "InlineModItem",
    "Item",
    "ModItem",
    "ProofItem",
    "Source",
    "parse_level",
)

_ESCAPES = {
    "\\": "\\",
    '"': '"',
    "n": "\n",
    "r": "\r",
    "t": "\t",
    "0": "\0",
}


@dc.dataclass(frozen=True, slots=True)
class Source:
    """One source file, original and masked.

    Attributes
    ----------
    original : str
        Source text, used for attribute values and line numbers.
    masked : str
        The same text with comments and literals blanked.
    """

    original: str
    masked: str


@dc.dataclass(frozen=True, slots=True)
class ProofItem:
    """A proof item found at one module level."""

    function: str
    line: int
    attributes: tuple[str, ...]
    conditional: bool


@dc.dataclass(frozen=True, slots=True)
class ModItem:
    """A ``mod name;`` declaration at one module level."""

    name: str
    line: int
    path_attribute: str | None
    conditional: bool


@dc.dataclass(frozen=True, slots=True)
class InlineModItem:
    """A ``mod name { ... }`` block at one module level."""

    name: str
    line: int
    conditional: bool
    items: tuple[Item, ...]
    path_attribute: str | None = None


type Item = ProofItem | ModItem | InlineModItem


@dc.dataclass(frozen=True, slots=True)
class _Attributes:
    """The attribute facts one item carries."""

    proof: tuple[str, ...] = ()
    path: str | None = None
    conditional: bool = False
    inner_conditional: bool = False


@dc.dataclass(slots=True)
class _AttributeFacts:
    """The accumulating facts of one item's attribute groups."""

    proof: list[str] = dc.field(default_factory=list)
    path: str | None = None
    conditional: bool = False
    inner_conditional: bool = False

    def record(self, original: str, masked: str) -> None:
        """Record one attribute's facts, ignoring inner attributes."""
        name, _ = attribute_name(masked)
        if name in PROOF_ATTRIBUTES:
            self.proof.append(name)
        if name == "path":
            # Only `#[path = "..."]` names a module file; a string-valued
            # attribute such as `#[doc = "..."]` must not redirect discovery.
            value = path_value(original)
            if value is None:
                msg = (
                    "the #[path] attribute "
                    f"{original.strip()!r} is not a supported string literal"
                )
                raise InventoryDiscoveryError(msg)
            self.path = value
        if name == "cfg" and normalise(masked) != "cfg(kani)":
            self.conditional = True
        if name == "cfg_attr":
            self._record_cfg_attr(masked, original)

    def _record_cfg_attr(self, masked: str, original: str) -> None:
        """Record the proof, condition, and path facts inside ``cfg_attr``."""
        nested = cfg_attr_facts(masked, original)
        if nested is None:
            return
        proofs, conditional, path = nested
        self.proof.extend(proofs)
        self.conditional = self.conditional or conditional
        if path is not None:
            self.path = path


def parse_level(
    source: Source,
    start: int,
    end: int,
    *,
    conditional: bool = False,
) -> tuple[Item, ...]:
    """Parse every item in one module level.

    Parameters
    ----------
    source : Source
        Original and masked text.
    start : int
        First character of the level.
    end : int
        One past the last character of the level.
    conditional : bool
        Whether an enclosing module already carries a non-Kani ``cfg``.

    Returns
    -------
    tuple[Item, ...]
        Proofs, file modules, and inline modules in source order.
    """
    items: list[Item] = []
    level_conditional = conditional
    position = start
    while position < end:
        position = _skip_space(source.masked, position)
        if position >= end:
            break
        attributes, after = _attributes(source, position)
        level_conditional = level_conditional or attributes.inner_conditional
        if after >= end:
            break
        keyword_at = _skip_modifiers(source.masked, after)
        word = identifier(source.masked, keyword_at)
        if word is None:
            position = _skip_item(source.masked, after)
            continue
        combined = _Attributes(
            attributes.proof,
            attributes.path,
            attributes.conditional or level_conditional,
        )
        item, position = _parse_item(source, after, word, combined)
        if item is not None:
            items.append(item)
    return tuple(items)


def _skip_space(masked: str, position: int) -> int:
    """Skip whitespace forward."""
    while position < len(masked) and masked[position].isspace():
        position += 1
    return position


def _parse_item(
    source: Source,
    after: int,
    word: Identifier,
    attributes: _Attributes,
) -> tuple[Item | None, int]:
    """Parse one item selected by its leading keyword."""
    if not word.is_raw and word.text == "mod":
        return _parse_mod(source, _skip_space(source.masked, word.end), attributes)
    if not word.is_raw and word.text == "fn":
        return _parse_fn(source, word, attributes)
    return None, _skip_item(source.masked, after)


def _parse_mod(
    source: Source,
    position: int,
    attributes: _Attributes,
) -> tuple[Item | None, int]:
    """Parse a ``mod name;`` or ``mod name { ... }`` declaration."""
    name_match = identifier(source.masked, position)
    if name_match is None:
        return None, _skip_item(source.masked, position)
    name = name_match.text
    after = _skip_space(source.masked, name_match.end)
    if after < len(source.masked) and source.masked[after] == "{":
        body_end = _matching(source.masked, after, "{", "}")
        children = parse_level(
            source,
            after + 1,
            body_end - 1,
            conditional=attributes.conditional,
        )
        return (
            InlineModItem(
                name,
                _line_of(source.original, position),
                attributes.conditional,
                children,
                attributes.path,
            ),
            body_end,
        )
    return (
        ModItem(
            name=name,
            line=_line_of(source.original, position),
            path_attribute=attributes.path,
            conditional=attributes.conditional,
        ),
        min(after + 1, len(source.masked)),
    )


def _parse_fn(
    source: Source,
    word: Identifier,
    attributes: _Attributes,
) -> tuple[Item | None, int]:
    """Parse a function, recording it when a proof attribute selects it."""
    after = _skip_space(source.masked, word.end)
    name_match = identifier(source.masked, after)
    position = _skip_item(
        source.masked,
        after if name_match is None else name_match.end,
    )
    if name_match is None or not attributes.proof:
        return None, position
    return (
        ProofItem(
            function=name_match.text,
            line=_line_of(source.original, after),
            attributes=attributes.proof,
            conditional=attributes.conditional,
        ),
        position,
    )


def _attributes(source: Source, position: int) -> tuple[_Attributes, int]:
    """Parse every attribute group starting at `position`."""
    facts = _AttributeFacts()
    masked = source.masked
    while position < len(masked) and masked.startswith("#", position):
        inner = False
        bracket = _skip_space(masked, position + 1)
        if bracket < len(masked) and masked[bracket] == "!":
            inner = True
            bracket = _skip_space(masked, bracket + 1)
        if bracket >= len(masked) or masked[bracket] != "[":
            break
        end = _matching(masked, bracket, "[", "]")
        content = masked[bracket + 1 : end - 1]
        if inner:
            inner_name, _ = attribute_name(content)
            if inner_name == "cfg" and normalise(content) != "cfg(kani)":
                facts.inner_conditional = True
        else:
            facts.record(
                source.original[bracket + 1 : end - 1],
                content,
            )
        position = _skip_space(masked, end)
    return (
        _Attributes(
            tuple(facts.proof),
            facts.path,
            facts.conditional,
            facts.inner_conditional,
        ),
        position,
    )


def _starts_string(masked: str, position: int) -> bool:
    """Whether a string literal starts at `position`."""
    return position < len(masked) and masked[position] == '"'


def _skip_modifiers(masked: str, position: int) -> int:
    """Skip visibility and item qualifiers before a keyword."""
    while position < len(masked):
        word = identifier(masked, position)
        if word is None or word.is_raw:
            return position
        value = word.text
        if value == "pub":
            position = _skip_space(masked, word.end)
            if position < len(masked) and masked[position] == "(":
                position = _matching(masked, position, "(", ")")
        elif value in {"unsafe", "async", "const", "default", "extern"}:
            position = word.end
            if value == "extern" and _starts_string(masked, position):
                position += 1
        else:
            return position
        position = _skip_space(masked, position)
    return position


def _skip_item(masked: str, position: int) -> int:
    """Return the position after one item, including a braced body."""
    index, terminator = _find_top_level(masked, position)
    if terminator == ";":
        return index + 1
    if terminator != "{":
        return len(masked)
    end = _skip_space(masked, _matching(masked, index, "{", "}"))
    if end < len(masked) and masked[end] == ";":
        end += 1
    return end


def _find_top_level(masked: str, position: int) -> tuple[int, str]:
    """Return the first top-level ``;`` or ``{`` and its character."""
    braces = parens = brackets = 0
    while position < len(masked):
        char = masked[position]
        if braces == parens == brackets == 0 and char in {";", "{"}:
            return position, char
        delta = DEPTH_DELTAS.get(char)
        if delta is not None:
            braces = max(0, braces + delta[0])
            parens = max(0, parens + delta[1])
            brackets = max(0, brackets + delta[2])
        position += 1
    return len(masked), ""


def _matching(masked: str, start: int, opening: str, closing: str) -> int:
    """Return the index just after the bracket matching the one at `start`."""
    depth = 0
    position = start
    while position < len(masked):
        char = masked[position]
        if char == opening:
            depth += 1
        elif char == closing:
            depth -= 1
            if depth == 0:
                return position + 1
        position += 1
    return len(masked)


def _line_of(text: str, position: int) -> int:
    """Return the one-based line of `position` in `text`."""
    return text.count("\n", 0, position) + 1
