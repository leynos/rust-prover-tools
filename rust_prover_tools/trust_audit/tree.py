"""Follow ``mod`` declarations from a set of entry points to real files.

The matcher decides what one source contains. This module supplies the two
things a single file does not have: a repository root, which turns a ``mod``
declaration into a real target, and a set of files, which turns one scan into
an audit.

Two policies live here rather than in the matcher, because both need that root:

- A module target outside the repository is refused. Following it would audit a
  file the repository does not own, and skipping it would silently leave out
  code that *is* compiled.
- The walk follows ``mod`` declarations rather than sweeping every ``.rs`` file
  under a directory. A file no ``mod`` names is not compiled, so scanning it
  would report items in dead code as though they were part of the proof.

A module target is canonicalized to the physical file it reaches, so a cyclic
``#[path]`` through a symbolic link is scanned once rather than walked under a
fresh spelling for ever. A file reached with two different owners is still
refused: its children would resolve beneath one directory or the other, and
the audit cannot say which files the crate compiles.

This is I/O, so it sits at the edge: everything it decides is a pure function
of a repository root and repository-relative paths.
"""

from __future__ import annotations

import dataclasses as dc
import pathlib
import typing as typ

from rust_prover_tools.trust_audit.errors import TrustAuditError
from rust_prover_tools.trust_audit.manifests import is_procedural_macro
from rust_prover_tools.trust_audit.matcher import scan
from rust_prover_tools.trust_audit.model import (
    AuditedFile,
    ModuleReference,
    TrustItem,
)
from rust_prover_tools.trust_audit.module_paths import (
    RUST_SUFFIX,
    Module,
    module_target,
    parent_directory,
    physical_relative,
    resolve_repository_path,
)
from rust_prover_tools.trust_audit.vocabulary import (
    PROVER_VOCABULARIES,
    Vocabulary,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

__all__ = ("RUST_SUFFIX", "audit_tree", "resolve_repository_path")


def audit_tree(
    root: pathlib.Path,
    drivers: cabc.Iterable[str],
    *,
    procedural_macro: bool | None = None,
    vocabularies: tuple[Vocabulary, ...] = PROVER_VOCABULARIES,
) -> tuple[AuditedFile, ...]:
    """Audit every Rust source reachable from the given entry points.

    Parameters
    ----------
    root : Path
        Repository root; every scanned file must resolve inside it.
    drivers : Iterable[str]
        Repository-relative Rust files to start from, typically each crate's
        ``lib.rs``, ``main.rs`` and integration-test roots.
    procedural_macro : bool | None
        Whether to scan string literals too, for a crate that expands them into
        code.  ``None``, the default, reads the flag from the nearest
        ``Cargo.toml`` above each driver; ``True`` or ``False`` states it for
        every file instead, so a caller can correct a manifest the audit cannot
        read.
    vocabularies : tuple[Vocabulary, ...]
        The prover vocabularies to apply.  Defaults to every known vocabulary;
        a caller narrows it to audit one prover.

    Returns
    -------
    tuple[AuditedFile, ...]
        One entry per scanned file, in path order, including files with no
        items so a caller can tell "audited and clean" from "never read".

    Raises
    ------
    TrustAuditError
        If a driver is missing, a module target leaves the repository or
        resolves to no file, a file cannot be read, or two paths reach one
        file under different spellings.

    Examples
    --------
    Given a crate root that declares ``mod child;``, the audit reports both
    ``src/lib.rs`` and ``src/child.rs``: the walk follows the declaration rather
    than listing the directory.
    """
    # Every driver is a crate root by definition: Cargo compiles each of them as
    # a crate root, so its declarations are searched for beside it rather than
    # in a subdirectory named after it — however Cargo chose to name the file.
    pending = [
        _driver_module(root, driver, procedural_macro=procedural_macro)
        for driver in drivers
    ]
    scanned: dict[str, tuple[TrustItem, ...]] = {}
    contexts: dict[str, tuple[str, str]] = {}
    policies: dict[str, bool] = {}
    macro_scopes: dict[str, bool] = {}
    while pending:
        module = pending.pop()
        # Every arrival is claimed before the deduplication below: skipping a
        # file already scanned would hide that it was reached a second way,
        # which is exactly the ambiguity ``_claim`` exists to refuse.
        _claim(root, module, contexts)
        first_arrival = module.relative not in scanned
        if not first_arrival and not _needs_rescan(
            scanned_as_procedural=policies[module.relative],
            required=module.procedural_macro,
            scanned_with_macro_use=macro_scopes[module.relative],
            requires_macro_use=module.macro_use,
        ):
            continue
        result = scan(
            _read_source(root, module.relative),
            procedural_macro=module.procedural_macro,
            vocabularies=vocabularies,
            inherited_macro_use=module.macro_use,
        )
        scanned[module.relative] = result.items
        policies[module.relative] = module.procedural_macro
        macro_scopes[module.relative] = module.macro_use
        # Re-enqueue after an upgrade too: a child first reached through
        # the plain crate must still receive the stronger policy.
        pending.extend(
            dc.replace(target, macro_use=module.macro_use or result.macro_use)
            for target in _module_targets(root, module, result.modules)
        )
    return tuple(AuditedFile(path, scanned[path]) for path in sorted(scanned))


def _needs_rescan(
    *,
    scanned_as_procedural: bool,
    required: bool,
    scanned_with_macro_use: bool,
    requires_macro_use: bool,
) -> bool:
    """Report whether a second arrival needs a stronger scan policy.

    One file can be reachable from a plain crate and from a proc-macro crate
    through ``#[path]``.  The procedural scan reads literals as code, so it is
    a superset; when the plain arrival was scanned first, the file must be
    rescanned rather than left with the weaker result.

    The same holds for inherited ``#[macro_use]`` scope.  A module two crates
    share is scanned once per arrival only while both arrivals agree; an
    arrival under an imported macro's scope sees invocations the earlier scan
    could not attribute, so the file and its children are rescanned.
    """
    if requires_macro_use and not scanned_with_macro_use:
        return True
    return required and not scanned_as_procedural


def _driver_module(
    root: pathlib.Path,
    driver: str,
    *,
    procedural_macro: bool | None,
) -> Module:
    """Build the module record for one entry point.

    The driver is reported and read as the physical file it reaches, so a path
    through a symbolic link names the compiled file.  Its children, though,
    resolve from the directory the compiler derived from the pathname it
    loaded, which is the spelling the caller supplied.  Its crate kind comes
    from the manifest above the driver itself, never from a module it later
    reaches through ``#[path]``.
    """
    spelling = _normalize(driver)
    return Module(
        relative=physical_relative(root, spelling),
        owner=parent_directory(spelling),
        directory=parent_directory(spelling),
        procedural_macro=(
            is_procedural_macro(root, driver)
            if procedural_macro is None
            else procedural_macro
        ),
    )


def _normalize(relative: str) -> str:
    r"""Return one repository-relative path in POSIX form.

    A backslash is an ordinary filename character, so it is preserved:
    replacing it with a separator would read ``src/proof/case.rs`` when the
    caller named ``src/proof\\case.rs``, and a clean file at the rewritten path
    would hide the trusted items in the file that was actually asked for.

    Components are preserved as written.  Collapsing ``..`` lexically would
    change which file the path names whenever a component is a symbolic link:
    ``src/link/../entry.rs`` names ``actual/entry.rs`` when ``src/link`` points
    at ``actual/nested``, not ``src/entry.rs``.  A path that escapes the root is
    refused by `resolve_repository_path` instead, which resolves it the way the
    filesystem does.
    """
    return pathlib.PurePosixPath(relative).as_posix()


def _claim(
    root: pathlib.Path,
    module: Module,
    contexts: cabc.MutableMapping[str, tuple[str, str]],
) -> None:
    """Record how ``module`` was reached, refusing an ambiguous context.

    A file reached twice through the same context is ordinary — two
    declarations may name one module — and is deduplicated by the caller.  A
    different owner, or a different declaring directory for a file-scope
    ``#[path]``, is not: its child modules would resolve beneath one directory
    or the other, so the audit cannot say which files the crate compiles.
    Both facts are compared after resolution, because two spellings such as
    ``plain/src/../shared`` and ``proc/src/../shared`` may name one directory.
    """
    key = (module.owner, module.directory)
    previous = contexts.setdefault(module.relative, key)
    if key != previous and _resolution_context(
        root,
        previous,
    ) != _resolution_context(root, key):
        msg = (
            f"{module.relative} is reached both as a file owning "
            f"{previous[0]!r} in {previous[1]!r} and as one owning "
            f"{module.owner!r} in {module.directory!r}; its child modules "
            "would resolve beneath different directories"
        )
        raise TrustAuditError(msg)


def _resolution_context(
    root: pathlib.Path,
    context: tuple[str, str],
) -> tuple[pathlib.Path, pathlib.Path]:
    """Return a context's owner and declaring directory, with links resolved."""
    owner, directory = context
    return (root / owner).resolve(), (root / directory).resolve()


def _read_source(root: pathlib.Path, relative: str) -> str:
    """Read one repository source as strict UTF-8, failing closed."""
    path = resolve_repository_path(root, relative)
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        msg = f"{relative} cannot be read: {error}"
        raise TrustAuditError(msg) from error


def _module_targets(
    root: pathlib.Path,
    module: Module,
    references: cabc.Iterable[ModuleReference],
) -> tuple[Module, ...]:
    """Resolve every module declaration a file makes to a target and owner."""
    return tuple(module_target(root, module, reference) for reference in references)
