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

The walk also refuses a path that reaches one file under two spellings, which
is how a cyclic ``#[path]`` presents itself. Auditing once and moving on would
resolve the file's children under whichever spelling happened to come first.

This is I/O, so it sits at the edge: everything it decides is a pure function
of a repository root and repository-relative paths.
"""

from __future__ import annotations

import pathlib
import posixpath
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
        Module(
            relative=_normalize(driver),
            owner=parent_directory(driver),
            directory=parent_directory(driver),
            procedural_macro=(
                is_procedural_macro(root, driver)
                if procedural_macro is None
                else procedural_macro
            ),
        )
        for driver in drivers
    ]
    scanned: dict[str, tuple[TrustItem, ...]] = {}
    owners: dict[str, str] = {}
    identities: dict[pathlib.Path, str] = {}
    while pending:
        module = pending.pop()
        # Every arrival is claimed before the deduplication below: skipping a
        # file already scanned would hide that it was reached a second way,
        # which is exactly the ambiguity ``_claim`` exists to refuse.
        _claim(root, module, owners, identities)
        if module.relative in scanned:
            continue
        result = scan(
            _read_source(root, module.relative),
            procedural_macro=module.procedural_macro,
            vocabularies=vocabularies,
        )
        scanned[module.relative] = result.items
        pending.extend(_module_targets(root, module, result.modules))
    return tuple(AuditedFile(path, scanned[path]) for path in sorted(scanned))


def _normalize(relative: str) -> str:
    r"""Return one repository-relative path in POSIX form.

    A backslash is an ordinary filename character, so it is preserved:
    replacing it with a separator would read ``src/proof/case.rs`` when the
    caller named ``src/proof\\case.rs``, and a clean file at the rewritten path
    would hide the trusted items in the file that was actually asked for.

    ``..`` and ``.`` components are collapsed lexically, without following a
    symbolic link, so a module reached through a ``#[path]`` is reported at the
    name a reader can find rather than as ``crate/src/../../shared/helper.rs``.
    Continuation is still checked by `resolve_repository_path`.
    """
    return posixpath.normpath(pathlib.PurePosixPath(relative).as_posix())


def _claim(
    root: pathlib.Path,
    module: Module,
    owners: cabc.MutableMapping[str, str],
    identities: cabc.MutableMapping[pathlib.Path, str],
) -> None:
    """Record how ``module`` was reached, refusing an ambiguous second way.

    A file reached twice through the same spelling is ordinary — two
    declarations may name one module — and is deduplicated by the caller. A
    file reached through two spellings is not: its child modules would resolve
    beneath one owner or the other, so the audit cannot say which files the
    crate compiles. Refusing is what makes a cyclic ``#[path]`` terminate
    rather than walk forever under fresh spellings of the same file.
    """
    owner = owners.setdefault(module.relative, module.owner)
    if owner != module.owner:
        msg = (
            f"{module.relative} is reached both as a file owning {owner!r} and "
            f"as one owning {module.owner!r}; its child modules would resolve "
            "beneath different directories"
        )
        raise TrustAuditError(msg)
    identity = identities.setdefault(
        resolve_repository_path(root, module.relative),
        module.relative,
    )
    if identity != module.relative:
        msg = (
            f"{module.relative} resolves to the same file as {identity}; the "
            "module walk refuses one file reached under two spellings, which "
            "is how a cyclic #[path] would recur"
        )
        raise TrustAuditError(msg)


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
