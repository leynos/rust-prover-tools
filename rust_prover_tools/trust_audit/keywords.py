"""Recognise the Rust keywords that can never be a path segment.

The lexer reports every keyword as an identifier, so a backward walk over a
path must not treat a preceding ``return`` or ``let`` as the segment in front
of a ``::``.  Only the path keywords ``crate``, ``self``, ``super`` and
``Self`` may appear there; every other keyword ends the path.
"""

from __future__ import annotations

from rust_prover_tools.trust_audit.syntax import Kind, Token

__all__ = ("is_path_segment",)

#: Keywords that cannot stand where a path segment stands.  The path keywords
#: ``crate``, ``self``, ``super`` and ``Self`` are deliberately absent: they
#: name a module in their own right.
_NON_PATH_KEYWORDS: frozenset[str] = frozenset(
    {
        "abstract",
        "as",
        "async",
        "await",
        "become",
        "box",
        "break",
        "const",
        "continue",
        "do",
        "dyn",
        "else",
        "enum",
        "extern",
        "false",
        "final",
        "fn",
        "for",
        "gen",
        "if",
        "impl",
        "in",
        "let",
        "loop",
        "macro",
        "match",
        "mod",
        "move",
        "mut",
        "override",
        "priv",
        "pub",
        "ref",
        "return",
        "static",
        "struct",
        "trait",
        "true",
        "try",
        "type",
        "typeof",
        "unsafe",
        "unsized",
        "use",
        "virtual",
        "where",
        "while",
        "yield",
    },
)


def is_path_segment(token: Token) -> bool:
    """Report whether ``token`` can continue a path as a segment.

    A raw identifier is always a segment: ``r#return`` is the name
    ``return``, not the keyword.  An unprefixed keyword is never one.
    """
    return token.kind is Kind.IDENT and (
        token.raw or token.text not in _NON_PATH_KEYWORDS
    )
