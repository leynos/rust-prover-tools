"""Hold the frozen values one audited source produces.

A scan reduces a file to two facts: the trusted items it contains, and the
module files it compiles. Each is a frozen record holding only text and
integers, so a scan compares and hashes exactly like the source it came from.
"""

from __future__ import annotations

import dataclasses as dc

__all__ = (
    "AuditedFile",
    "InlineModule",
    "ModuleReference",
    "ScanResult",
    "TrustItem",
)


@dc.dataclass(frozen=True, slots=True)
class TrustItem:
    """One trusted item, located by line and column in its file.

    The location is the item's own token, not its enclosing statement, so two
    uses on one line stay distinguishable.

    ``item`` is the normalized identifier the item is reported under, and it
    does not retain the qualification. A qualified ``kani::assume`` and a bare
    ``assume`` reached through an import both yield ``"assume"``, and a raw
    ``r#assume`` loses its prefix. What separates them is ``line`` and
    ``column``: they locate the original occurrence, so a consumer that needs
    the qualifier reads the source at that position.
    """

    item: str
    line: int
    column: int


@dc.dataclass(frozen=True, slots=True)
class InlineModule:
    """One inline ``mod name { … }`` an enclosing declaration sits inside.

    ``path`` is the module's own ``#[path = "…"]`` value, when it has one. Rust
    replaces that module's directory component with the attribute's value, so a
    bare ``mod x;`` inside ``#[path = "thread_files"] mod thread { … }``
    compiles ``thread_files/x.rs`` and not ``thread/x.rs``.
    """

    name: str
    path: str | None


@dc.dataclass(frozen=True, slots=True)
class ModuleReference:
    """A module file this source compiles, for the caller to resolve and scan.

    ``path`` is set when a ``#[path = "…"]`` attribute names the file directly;
    otherwise the caller derives it from ``name`` by Rust's usual search.

    ``enclosing`` lists the inline modules the declaration sits inside,
    outermost first. Each entry keeps that module's own ``#[path]`` value,
    because the attribute replaces the module's directory component: it is what
    separates ``mod nested { mod trusted; }`` in ``src/lib.rs`` — which compiles
    ``src/nested/trusted.rs`` — from a top-level ``mod trusted;``, which
    compiles ``src/trusted.rs``.
    """

    name: str
    path: str | None
    line: int
    column: int
    enclosing: tuple[InlineModule, ...] = ()


@dc.dataclass(frozen=True, slots=True)
class ScanResult:
    """Everything one source file contributes to an audit."""

    items: tuple[TrustItem, ...]
    modules: tuple[ModuleReference, ...]


@dc.dataclass(frozen=True, slots=True)
class AuditedFile:
    """One scanned file, named by the repository-relative path it was read as."""

    path: str
    items: tuple[TrustItem, ...]
