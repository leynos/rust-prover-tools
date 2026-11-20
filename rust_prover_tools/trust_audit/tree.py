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

import dataclasses as dc
import pathlib
import typing as typ

from rust_prover_tools.trust_audit.errors import TrustAuditError
from rust_prover_tools.trust_audit.matcher import scan
from rust_prover_tools.trust_audit.model import (
    AuditedFile,
    InlineModule,
    ModuleReference,
    TrustItem,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

__all__ = ("RUST_SUFFIX", "audit_tree", "resolve_repository_path")

RUST_SUFFIX: typ.Final[str] = ".rs"
# The files Rust searches for a bare `mod x;` beneath the owning directory: a
# file first, then the module directory's own `mod.rs`.
_MODULE_FILE_CANDIDATES: typ.Final[tuple[str, ...]] = (
    "{name}{suffix}",
    "{name}/mod{suffix}",
)


@dc.dataclass(frozen=True, slots=True)
class _Module:
    """A file the walk has yet to scan and the directory its children live in.

    The owner is not derivable from the file's own name. A file loaded by
    ``#[path]`` owns the directory it sits in, while a file loaded by bare
    ``mod x;`` owns the directory ``x`` beneath its declaring file's owner, so
    the walk carries the answer rather than guessing it again.
    """

    relative: str
    owner: str


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
        _Module(_normalise(driver), _parent_directory(driver)) for driver in drivers
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
            procedural_macro=procedural_macro,
        )
        scanned[module.relative] = result.items
        pending.extend(_module_targets(root, module, result.modules))
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


def _claim(
    root: pathlib.Path,
    module: _Module,
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
    module: _Module,
    references: cabc.Iterable[ModuleReference],
) -> tuple[_Module, ...]:
    """Resolve every module declaration a file makes to a target and owner."""
    return tuple(_module_target(root, module, reference) for reference in references)


def _module_target(
    root: pathlib.Path,
    module: _Module,
    reference: ModuleReference,
) -> _Module:
    """Resolve one module declaration, refusing a target outside the root.

    A ``#[path]`` target is relative to the declaring file's directory, as Rust
    resolves it; otherwise the module is searched for beneath its owner. A
    module that resolves to no file is a structural failure: the crate would not
    compile, and the audit would report a clean scan of code it never read.
    """
    candidates, owner = _module_candidates(module, reference)
    # Containment is checked before existence so an escaping `#[path]` is
    # refused for what it is. Reporting only that the file is absent would
    # invite someone to create it outside the repository and call the audit
    # satisfied.
    if not all(_is_inside(root, candidate) for candidate in candidates):
        msg = (
            f"{module.relative}:{reference.line}:{reference.column} declares "
            f"module {reference.name!r}, which resolves outside the "
            f"repository: {list(candidates)}"
        )
        raise TrustAuditError(msg)
    found = next(
        (candidate for candidate in candidates if (root / candidate).is_file()),
        None,
    )
    if found is None:
        msg = (
            f"{module.relative}:{reference.line}:{reference.column} declares "
            f"module {reference.name!r}, which resolves to none of "
            f"{list(candidates)}"
        )
        raise TrustAuditError(msg)
    return _Module(found, owner)


def _module_candidates(
    module: _Module,
    reference: ModuleReference,
) -> tuple[tuple[str, ...], str]:
    """Return the paths one declaration may name and the owner of the target.

    A path attribute names one file, and the loaded file owns the directory it
    sits in: rustc resolves its children from there, not from the module path
    the declaration would have had. A bare declaration is searched for beneath
    the declaration's owner, which may be an inline module's directory, and the
    loaded file owns the directory named after it — including when the file it
    found is that directory's ``mod.rs``.
    """
    if reference.path is not None:
        base = pathlib.PurePosixPath(_path_base(module, reference))
        candidate = (base / reference.path).as_posix()
        return (candidate,), pathlib.PurePosixPath(candidate).parent.as_posix()
    directory = pathlib.PurePosixPath(_search_directory(module, reference))
    candidates = tuple(
        (
            directory / template.format(name=reference.name, suffix=RUST_SUFFIX)
        ).as_posix()
        for template in _MODULE_FILE_CANDIDATES
    )
    return candidates, (directory / reference.name).as_posix()


def _path_base(module: _Module, reference: ModuleReference) -> str:
    """Return the directory a ``#[path]`` value is relative to.

    At file scope the value is relative to the declaring file's own directory —
    ``src/a/b.rs`` writing ``#[path = "foo.rs"]`` means ``src/a/foo.rs`` — so
    the declaring file's owner directory plays no part.

    Inside ``mod inline { … }`` the value is relative to that inline module's
    directory, which is the declaring file's owner extended by each inline name
    or ``#[path]`` value.
    """
    if not reference.enclosing:
        return _parent_directory(module.relative)
    return _inline_directory(module.owner, reference.enclosing)


def _search_directory(module: _Module, reference: ModuleReference) -> str:
    """Return the directory a bare ``mod name;`` is searched beneath.

    An inline module is a module in its own right, so its declarations are
    searched for beneath its name — or beneath its own ``#[path]`` value, which
    replaces the directory component entirely.
    """
    if not reference.enclosing:
        return module.owner
    return _inline_directory(module.owner, reference.enclosing)


def _inline_directory(
    owner: str,
    enclosing: cabc.Iterable[InlineModule],
) -> str:
    """Return the directory an inline chain contributes beneath its file.

    Each entry contributes one component: the inline module's own ``#[path]``
    value when it has one, otherwise its name. Rust's rule is that the attribute
    *replaces* the directory the inline module would have occupied, which is why
    the value is substituted rather than appended — appending would search
    ``thread/thread_files/x.rs`` for a file the crate loads from
    ``thread_files/x.rs``. A ``#[path]`` with more than one component is applied
    as written, matching Rust, which treats the value as a relative path.
    """
    components = [owner]
    for inline in enclosing:
        if inline.path is not None:
            components.extend(pathlib.PurePosixPath(inline.path).parts)
        else:
            components.append(inline.name)
    return pathlib.PurePosixPath(*components).as_posix()


def _parent_directory(relative: str) -> str:
    """Return the directory holding a repository-relative file."""
    return pathlib.PurePosixPath(relative).parent.as_posix()


def _is_inside(root: pathlib.Path, relative: str) -> bool:
    """Report whether a repository-relative path stays beneath ``root``."""
    try:
        resolve_repository_path(root, relative)
    except TrustAuditError:
        return False
    return True
