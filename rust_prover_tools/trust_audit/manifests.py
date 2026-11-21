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
            resolved = manifest.resolve()
            if resolved_root not in resolved.parents:
                msg = (
                    f"{manifest} resolves outside the repository root "
                    f"{resolved_root}, so its crate kind is unknown"
                )
                raise TrustAuditError(msg)
            return _declares_procedural_macro(resolved)
        if directory == resolved_root or resolved_root not in directory.parents:
            return False
        directory = directory.parent


def _declares_procedural_macro(manifest: pathlib.Path) -> bool:
    """Report whether one Cargo manifest declares a proc-macro library.

    Cargo accepts two declarations.  ``[lib] proc-macro = true`` is the
    documented flag, and ``crate-type = ["proc-macro"]`` names the same crate
    type through the general field; a manifest using either builds a proc-macro
    crate, so the audit must scan its literals.  The underscore spellings
    ``proc_macro`` and ``crate_type`` are deprecated aliases Cargo still honours
    outside the 2024 edition; reading them is the fail-closed choice, because
    missing the flag leaves literal-generated code unread.  A non-boolean
    ``proc-macro`` value or a non-string crate type is refused rather than
    guessed at.
    """
    try:
        document = tomllib.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        msg = f"{manifest} cannot be read, so the crate kind is unknown: {error}"
        raise TrustAuditError(msg) from error
    library = document.get("lib")
    if not isinstance(library, dict):
        return False
    # Cargo reads ``crate-type`` first: when it is present it decides the crate
    # type, and the ``proc-macro`` flag is only consulted when it is absent.
    crate_types = _manifest_value(library, "crate-type", "crate_type")
    if crate_types is not None:
        return _declares_crate_type(manifest, crate_types)
    flag = _manifest_value(library, "proc-macro", "proc_macro")
    if flag is None:
        return False
    if not isinstance(flag, bool):
        msg = f"{manifest} sets [lib] proc-macro to a non-boolean value"
        raise TrustAuditError(msg)
    return flag


def _declares_crate_type(manifest: pathlib.Path, crate_types: object) -> bool:
    """Report whether a ``crate-type`` value names a proc-macro library."""
    if isinstance(crate_types, str):
        return crate_types == "proc-macro"
    if isinstance(crate_types, list):
        if not all(isinstance(item, str) for item in crate_types):
            msg = f"{manifest} sets [lib] crate-type to a non-string entry"
            raise TrustAuditError(msg)
        return "proc-macro" in crate_types
    msg = f"{manifest} sets [lib] crate-type to an unexpected value"
    raise TrustAuditError(msg)


def _manifest_value(
    library: dict[str, object],
    documented: str,
    deprecated: str,
) -> object | None:
    """Return a value written under its documented or deprecated key.

    Cargo's deprecated underscore spellings are still honoured before the 2024
    edition.  A manifest writing both spellings is ambiguous, so it is refused
    rather than read one way.
    """
    first = library.get(documented)
    second = library.get(deprecated)
    if first is not None and second is not None:
        msg = (
            f"the manifest sets both {documented!r} and its deprecated "
            f"spelling {deprecated!r}"
        )
        raise TrustAuditError(msg)
    return first if first is not None else second
