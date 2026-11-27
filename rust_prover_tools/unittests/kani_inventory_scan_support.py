"""Shared synthetic-crate setup for the lexical scanner tests."""

from __future__ import annotations

import typing as typ

from rust_prover_tools.kani_inventory import collect_proofs

if typ.TYPE_CHECKING:
    from pathlib import Path


def write_lib(tmp_path: Path, body: str) -> Path:
    """Write ``src/lib.rs`` for one synthetic crate and return its path."""
    root = tmp_path / "src"
    root.mkdir(parents=True, exist_ok=True)
    crate = root / "lib.rs"
    crate.write_text(body, encoding="utf-8")
    return crate


def scan(tmp_path: Path, body: str) -> list[tuple[str, bool]]:
    """Return ``(pretty name, conditional)`` for one synthetic crate root."""
    return [
        (proof.pretty_name, proof.conditional)
        for proof in collect_proofs(write_lib(tmp_path, body), tmp_path)
    ]
