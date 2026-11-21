"""Read the crate kind a Cargo manifest declares.

The trust audit reads string literals as code only for a proc-macro crate,
because such a crate can turn a literal into an item the lexer never sees.
Cargo records that fact in the manifest, so this module answers it from the
nearest ``Cargo.toml`` rather than guessing from a file's name or path.

This module reads the filesystem; `rust_prover_tools.trust_audit.tree` is the
only other module that does.
"""

from __future__ import annotations

import tomllib
import typing as typ

from rust_prover_tools.trust_audit.errors import TrustAuditError

if typ.TYPE_CHECKING:
    import pathlib

__all__ = ("CARGO_MANIFEST", "is_procedural_macro")

#: The manifest that says whether a crate is a proc-macro crate.
CARGO_MANIFEST: str = "Cargo.toml"


def is_procedural_macro(root: pathlib.Path, relative: str) -> bool:
    """Report whether the crate owning ``relative`` is a proc-macro crate.

    A manifest the audit cannot parse is refused: it says which crate kinds
    exist, and guessing would decide whether literals are scanned as code from
    a file nobody read.  A file with no manifest above it belongs to no crate
    Cargo builds, so it is not a proc-macro crate.

    Parameters
    ----------
    root : Path
        Repository root, which bounds the search.
    relative : str
        Repository-relative file whose owning crate is asked about.

    Returns
    -------
    bool
        Whether the nearest manifest declares a proc-macro crate.

    Raises
    ------
    TrustAuditError
        If the nearest manifest exists but cannot be read or parsed.
    """
    resolved_root = root.resolve()
    directory = (resolved_root / relative).parent
    while True:
        manifest = directory / CARGO_MANIFEST
        if manifest.is_file():
            return _declares_procedural_macro(manifest)
        if directory == resolved_root or resolved_root not in directory.parents:
            return False
        directory = directory.parent


def _declares_procedural_macro(manifest: pathlib.Path) -> bool:
    """Report whether one Cargo manifest declares a proc-macro library.

    A ``[lib]`` table with ``proc-macro = true`` is the only declaration Cargo
    honours.  ``proc_macro`` without the hyphen is a different key, so reading
    it as the flag would change what the audit scans for a manifest that never
    asked for it.
    """
    try:
        document = tomllib.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        msg = f"{manifest} cannot be read, so the crate kind is unknown: {error}"
        raise TrustAuditError(msg) from error
    library = document.get("lib")
    if not isinstance(library, dict):
        return False
    return library.get("proc-macro") is True
