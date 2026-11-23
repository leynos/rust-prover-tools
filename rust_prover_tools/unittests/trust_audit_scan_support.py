"""Shared projection for the trust-scan test modules."""

from __future__ import annotations

from rust_prover_tools.trust_audit import scan


def items(source: str, *, procedural_macro: bool = False) -> list[tuple[str, int]]:
    """Return each reported item with the line it was found on."""
    return [
        (item.item, item.line)
        for item in scan(source, procedural_macro=procedural_macro).items
    ]
