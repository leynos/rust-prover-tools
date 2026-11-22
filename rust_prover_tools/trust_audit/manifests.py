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

#: Cargo packages whose names a manifest may rename to another key.
_TRUST_PACKAGES: frozenset[str] = frozenset({"kani", "vstd", "verus_builtin"})


def is_procedural_macro(root: pathlib.Path, relative: str) -> bool:
    """Report whether the crate owning ``relative`` is a proc-macro crate.

    A manifest the audit cannot parse is refused: it says which crate kinds
    exist, and guessing would decide whether literals are scanned as code from
    a file nobody read.  A file with no manifest above it belongs to no crate
    whose kind Cargo can declare, so it is not a proc-macro crate.  A driver
    listed by a manifest's ``[lib] path`` is owned by that manifest even when
    it sits outside the manifest's directory; that declaration is searched for
    repository-wide before the answer falls back to false.

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
    leaf = (resolved_root / relative).resolve()
    directory = leaf.parent
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
            return _declares_procedural_macro(resolved) or _manifest_covers(
                resolved_root,
                resolved,
                leaf,
            )
        if directory == resolved_root or resolved_root not in directory.parents:
            return _manifest_naming_leaf(resolved_root, leaf)
        directory = directory.parent


def _manifest_naming_leaf(root: pathlib.Path, leaf: pathlib.Path) -> bool:
    """Report whether any manifest in ``root`` declares ``leaf`` as its library.

    Cargo resolves ``[lib] path`` relative to its manifest, so a driver can sit
    outside that manifest's directory.  Searching upward from the driver then
    finds nothing, and reporting false would leave a proc-macro crate's
    literals unread.  Every manifest in the repository is asked instead.
    """
    for manifest in sorted(root.rglob(CARGO_MANIFEST)):
        resolved = manifest.resolve()
        if root not in resolved.parents:
            continue
        if _manifest_covers(root, resolved, leaf):
            return True
    return False


def _manifest_covers(
    root: pathlib.Path,
    manifest: pathlib.Path,
    leaf: pathlib.Path,
) -> bool:
    """Report whether ``manifest``'s ``[lib] path`` names ``leaf``."""
    declared = _library_path(manifest)
    if declared is None:
        return False
    target = (manifest.parent / declared).resolve()
    if root not in target.parents and target != root:
        return False
    return target == leaf


def _library_path(manifest: pathlib.Path) -> str | None:
    """Return a manifest's ``[lib] path`` value, if it declares one."""
    try:
        document = tomllib.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        msg = f"{manifest} cannot be read, so the crate kind is unknown: {error}"
        raise TrustAuditError(msg) from error
    _refuse_renamed_trust_package(manifest, document)
    library = document.get("lib")
    if not isinstance(library, dict):
        return None
    value = library.get("path")
    if value is None:
        return None
    if not isinstance(value, str):
        msg = f"{manifest} sets [lib] path to a non-string value"
        raise TrustAuditError(msg)
    return value


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
    _refuse_renamed_trust_package(manifest, document)
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


def _refuse_renamed_trust_package(
    manifest: pathlib.Path,
    document: dict[str, object],
) -> None:
    """Refuse a manifest that renames a trust crate as a dependency.

    Cargo lets ``proof = { package = "kani", ... }`` bind the crate under a new
    name without any import, and the per-file scan cannot see that rename.  A
    call through the new name would pass unreported, so the audit refuses the
    manifest rather than a file it cannot read.
    """
    sections: list[object] = [
        document.get("dependencies"),
        document.get("dev-dependencies"),
        document.get("build-dependencies"),
    ]
    workspace = document.get("workspace")
    if isinstance(workspace, dict):
        sections.append(workspace.get("dependencies"))
    for table in sections:
        if not isinstance(table, dict):
            continue
        for name, spec in table.items():
            if not isinstance(spec, dict):
                continue
            package = spec.get("package")
            if package in _TRUST_PACKAGES:
                msg = (
                    f"{manifest} renames the Cargo package {package!r} to "
                    f"{name!r}, so the audit cannot tell which calls reach it"
                )
                raise TrustAuditError(msg)


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
