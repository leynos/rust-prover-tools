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

This is I/O, so it sits at the edge: everything it decides is a pure function
of a repository root and repository-relative paths.
"""

from __future__ import annotations

import pathlib
import typing as typ

from rust_prover_tools.trust_audit.errors import TrustAuditError
from rust_prover_tools.trust_audit.matcher import scan
from rust_prover_tools.trust_audit.model import (
    AuditedFile,
    InlineModule,
    ModuleReference,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

__all__ = ("RUST_SUFFIX", "audit_tree", "resolve_repository_path")

RUST_SUFFIX: typ.Final[str] = ".rs"
# The files Rust searches for a bare `mod x;`: a file first, then the module
# directory's own `mod.rs`.
_MODULE_FILE_CANDIDATES: typ.Final[tuple[str, ...]] = (
    "{directory}/{name}{suffix}",
    "{directory}/{name}/mod{suffix}",
)
# The stems that own their directory's children directly rather than a
# subdirectory named after themselves.
_ROOT_MODULE_STEMS: typ.Final[frozenset[str]] = frozenset({"lib", "main", "mod"})


def audit_tree(
    root: pathlib.Path,
    drivers: cabc.Iterable[str],
    *,
    procedural_macro: bool = False,
) -> tuple[AuditedFile, ...]:
    """Audit every Rust source reachable from the given entry points.

    Parameters
    ----------
    root : Path
        Repository root; every scanned file must resolve inside it.
    drivers : Iterable[str]
        Repository-relative Rust files to start from, typically each crate's
        ``lib.rs``, ``main.rs`` and integration-test roots.
    procedural_macro : bool
        Scan string literals too, for a crate that expands them into code.

    Returns
    -------
    tuple[AuditedFile, ...]
        One entry per scanned file, in path order, including files with no
        items so a caller can tell "audited and clean" from "never read".

    Raises
    ------
    TrustAuditError
        If a driver is missing, a module target leaves the repository or
        resolves to no file, or a file cannot be read.

    Examples
    --------
    Given a crate root that declares ``mod child;``, the audit reports both
    ``src/lib.rs`` and ``src/child.rs``: the walk follows the declaration rather
    than listing the directory.
    """
    scanned = {}
    # Every driver is a crate root by definition: the caller names `lib.rs`,
    # `main.rs` and each integration-test file, and Cargo compiles the last of
    # those as a crate root even though its stem is neither of the other two.
    crate_roots = frozenset(_normalise(driver) for driver in drivers)
    # A declaration may name a file that declares further modules or is named
    # twice through two paths, so the queue is deduplicated and the walk may
    # revisit a file it has already scanned.
    pending = list(dict.fromkeys(drivers))
    while pending:
        relative = _normalise(pending.pop())
        if relative in scanned:
            continue
        result = scan(_read_source(root, relative), procedural_macro=procedural_macro)
        scanned[relative] = result.items
        pending.extend(
            _module_targets(
                root,
                relative,
                result.modules,
                crate_root=relative in crate_roots,
            ),
        )
    return tuple(AuditedFile(path, scanned[path]) for path in sorted(scanned))


def resolve_repository_path(root: pathlib.Path, relative: str) -> pathlib.Path:
    """Resolve a repository-relative path, refusing anything outside the root.

    Containment is checked on the resolved path, so ``a/../../b`` cannot borrow
    the root's prefix to escape it.

    Parameters
    ----------
    root : Path
        Repository root.
    relative : str
        Repository-relative path.

    Returns
    -------
    Path
        The resolved absolute path.

    Raises
    ------
    TrustAuditError
        If ``relative`` is absolute or resolves outside ``root``.

    Examples
    --------
    ``src/lib.rs`` resolves beneath the root; ``../elsewhere.rs`` is refused.
    """
    if pathlib.PurePosixPath(relative).is_absolute():
        msg = f"{relative} is absolute; the audit reads only repository files"
        raise TrustAuditError(msg)
    resolved = (root / relative).resolve()
    root_resolved = root.resolve()
    if resolved != root_resolved and root_resolved not in resolved.parents:
        msg = f"{relative} resolves outside the repository"
        raise TrustAuditError(msg)
    return resolved


def _normalise(relative: str) -> str:
    r"""Return one repository-relative path in POSIX form.

    A backslash is an ordinary filename character, so it is preserved:
    replacing it with a separator would read ``src/proof/case.rs`` when the
    caller named ``src/proof\\case.rs``, and a clean file at the rewritten path
    would hide the trusted items in the file that was actually asked for.
    """
    return pathlib.PurePosixPath(relative).as_posix()


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
    relative: str,
    modules: cabc.Iterable[ModuleReference],
    *,
    crate_root: bool,
) -> tuple[str, ...]:
    """Resolve every module declaration a file makes to a repository path.

    A ``#[path]`` target is relative to the naming file, as Rust resolves it;
    otherwise the module is searched for beside the file and in its own
    directory. A module that resolves to no file is a structural failure: the
    crate would not compile, and the audit would report a clean scan of code it
    never read.
    """
    return tuple(
        _module_target(root, relative, module, crate_root=crate_root)
        for module in modules
    )


def _module_target(
    root: pathlib.Path,
    relative: str,
    module: ModuleReference,
    *,
    crate_root: bool,
) -> str:
    """Resolve one module declaration, refusing a target outside the root."""
    if module.path:
        candidates = _path_candidates(relative, module, crate_root=crate_root)
    else:
        candidates = _search_candidates(relative, module, crate_root=crate_root)
    # Containment is checked before existence so an escaping `#[path]` is
    # refused for what it is. Reporting only that the file is absent would
    # invite someone to create it outside the repository and call the audit
    # satisfied.
    if not all(_is_inside(root, candidate) for candidate in candidates):
        msg = (
            f"{relative}:{module.line}:{module.column} declares module "
            f"{module.name!r}, which resolves outside the repository: "
            f"{list(candidates)}"
        )
        raise TrustAuditError(msg)
    found = next(
        (candidate for candidate in candidates if (root / candidate).is_file()),
        None,
    )
    if found is None:
        msg = (
            f"{relative}:{module.line}:{module.column} declares module "
            f"{module.name!r}, which resolves to none of {list(candidates)}"
        )
        raise TrustAuditError(msg)
    return found


def _is_inside(root: pathlib.Path, relative: str) -> bool:
    """Report whether a repository-relative path stays beneath ``root``."""
    try:
        resolve_repository_path(root, relative)
    except TrustAuditError:
        return False
    return True


def _path_candidates(
    relative: str,
    module: ModuleReference,
    *,
    crate_root: bool,
) -> tuple[str, ...]:
    """Return the single path a ``#[path]`` attribute names.

    At file scope the target is relative to the declaring file's own directory —
    ``src/a/b.rs`` writing ``#[path = "foo.rs"]`` means ``src/a/foo.rs`` — so
    the declaring file's module directory plays no part.

    Inside ``mod inline { … }`` the target is relative to that inline module's
    directory: the mod-rs file's directory plus the inline names, preceded by
    the file's own module directory when it is not a mod-rs file. That is
    exactly what `_search_candidates` applies to a bare ``mod name;``, with
    each inline module's own ``#[path]`` substituted for its name.

    Ignoring the chain is not a refusal a caller can notice: the declaration
    resolves to some file either way, so where the shallow path exists as a
    decoy the audit reports a clean scan of code the crate never compiles.
    """
    if not module.enclosing:
        parent = pathlib.PurePosixPath(relative).parent
        return ((parent / str(module.path)).as_posix(),)
    directory = _enclosing_directory(
        relative,
        module.enclosing,
        crate_root=crate_root,
    )
    return ((directory / str(module.path)).as_posix(),)


def _search_candidates(
    relative: str,
    module: ModuleReference,
    *,
    crate_root: bool,
) -> tuple[str, ...]:
    """Return the paths Rust searches for a bare ``mod name;``.

    The directory is not simply the declaring file's own. Rust puts a child of a
    *non-root* module in a directory named after that module, so ``src/lib.rs``
    declaring ``mod foo;`` searches ``src/foo.rs`` while ``src/foo.rs``
    declaring ``mod bar;`` searches ``src/foo/bar.rs``. Reading the declaring
    file's own directory for the second case would find an unrelated
    ``src/bar.rs`` and audit it as though it were compiled into the module that
    declared it.

    A ``mod x;`` written inside ``mod nested { … }`` is looked for under
    ``nested/``, because the inline module is a module in its own right, and an
    inline module carrying ``#[path = "…"]`` contributes that value instead of
    its name, because the attribute replaces the directory component.
    """
    directory = _enclosing_directory(
        relative,
        module.enclosing,
        crate_root=crate_root,
    ).as_posix()
    return tuple(
        template.format(directory=directory, name=module.name, suffix=RUST_SUFFIX)
        for template in _MODULE_FILE_CANDIDATES
    )


def _enclosing_directory(
    relative: str,
    enclosing: cabc.Iterable[InlineModule],
    *,
    crate_root: bool,
) -> pathlib.PurePosixPath:
    """Return the directory an inline chain contributes beneath the declaring file.

    Each entry contributes one component: the inline module's own ``#[path]``
    value when it has one, otherwise its name. Rust's rule is that the attribute
    *replaces* the directory the inline module would have occupied, which is why
    the value is substituted rather than appended — appending would search
    ``thread/thread_files/x.rs`` for a file the crate loads from
    ``thread_files/x.rs``. A ``#[path]`` with more than one component is applied
    as written, matching Rust, which treats the value as a relative path.
    """
    components: list[str] = [_module_directory(relative, crate_root=crate_root)]
    for inline in enclosing:
        if inline.path is not None:
            components.extend(pathlib.PurePosixPath(inline.path).parts)
        else:
            components.append(inline.name)
    return pathlib.PurePosixPath(*components)


def _module_directory(relative: str, *, crate_root: bool) -> str:
    """Return the directory Rust searches for a module declared in ``relative``.

    A crate root owns its directory's children directly. That is normally
    visible from the stem — ``lib.rs``, ``main.rs`` and an ``mod.rs`` — but an
    integration test is a crate root whatever Cargo chose to call it, so the
    caller passes that fact in: ``tests/proof.rs`` declaring ``mod helper;``
    compiles ``tests/helper.rs`` and not ``tests/proof/helper.rs``.

    Any other file is a module whose children live in a directory named after
    it, so ``src/foo.rs`` contributes ``src/foo`` and ``src/foo/bar.rs``
    contributes ``src/foo/bar``.
    """
    path = pathlib.PurePosixPath(relative)
    if crate_root or path.stem in _ROOT_MODULE_STEMS:
        return path.parent.as_posix()
    return (path.parent / path.stem).as_posix()
