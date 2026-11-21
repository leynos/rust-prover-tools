"""Register the exact Kani releases this package can read and select.

A grammar is qualified against one release at a time: the corpus behind
:mod:`rust_prover_tools.kani_log.syntax` was captured from Kani 0.67.0, and a
different release may print checks, counts, or wording that grammar does not
cover. Widening the registry is therefore a deliberate act that requires a
captured pass, counterexample, no-match, and truncated log for the new release.

The registry also states what the library adds to every invocation and what a
caller may not supply. The added output argument forces the regular text the
grammar reads. The forbidden spellings are the options that would move
evidence out of the parsed stream, suppress it, or override the supervisor's
exact selection.

Examples
--------
>>> grammar = grammar_for("0.67.0")
>>> grammar.output_arguments
('--output-format=regular',)
>>> pretty_name(("harnesses",), "verified")
'harnesses::verified'
"""

from __future__ import annotations

import dataclasses as dc
import typing as typ

from rust_prover_tools.kani_log.syntax import UnsupportedKaniGrammarError

if typ.TYPE_CHECKING:
    import collections.abc as cabc

__all__ = (
    "KANI_067",
    "SUPPORTED_VERSIONS",
    "ForbiddenOption",
    "KaniGrammar",
    "grammar_for",
    "pretty_name",
    "supported_versions",
)


@dc.dataclass(frozen=True, slots=True)
class ForbiddenOption:
    """One option spelling a supervised invocation must reject.

    Attributes
    ----------
    long : str
        Long spelling, such as ``--harness``.
    short : str | None
        Short spelling, such as ``-p``, when the upstream option has one.
    takes_value : bool
        Whether the option consumes a value, which decides whether the
        ``--option=value`` and ``-pvalue`` forms also have to be rejected.
    category : str
        ``selector`` or ``output``, naming why the option is refused.
    """

    long: str
    short: str | None
    takes_value: bool
    category: str


@dc.dataclass(frozen=True, slots=True)
class KaniGrammar:
    """The registered facts for one exact Kani release.

    Attributes
    ----------
    version : str
        Exact semantic version the grammar was qualified against.
    grammar_revision : str
        Where the grammar came from, for provenance in reports.
    output_arguments : tuple[str, ...]
        Arguments the library adds to force the output the grammar reads.
    forbidden_options : tuple[ForbiddenOption, ...]
        Selector and output spellings a caller may not supply.
    """

    version: str
    grammar_revision: str
    output_arguments: tuple[str, ...]
    forbidden_options: tuple[ForbiddenOption, ...]


def pretty_name(module_path: cabc.Sequence[str], function: str) -> str:
    """Derive the fully qualified Kani pretty name of one harness.

    Parameters
    ----------
    module_path : cabc.Sequence[str]
        Module segments relative to the crate root, outermost first. An empty
        sequence means the harness sits in the crate root.
    function : str
        Rust function name.

    Returns
    -------
    str
        ``module::submodule::function``, or just ``function`` at the root.
    """
    return "::".join((*module_path, function))


KANI_067: typ.Final[KaniGrammar] = KaniGrammar(
    version="0.67.0",
    grammar_revision="combobulate-9dd6e3c",
    # Kani 0.67.0 defaults to `regular`, but a repository's Cargo configuration
    # can select another style. Naming it explicitly keeps the grammar's
    # assumption true without changing proof meaning.
    output_arguments=("--output-format=regular",),
    forbidden_options=(
        ForbiddenOption("--package", "-p", takes_value=True, category="selector"),
        ForbiddenOption("--harness", None, takes_value=True, category="selector"),
        ForbiddenOption("--exact", None, takes_value=False, category="selector"),
        ForbiddenOption("--tests", None, takes_value=False, category="selector"),
        ForbiddenOption("--workspace", None, takes_value=False, category="selector"),
        ForbiddenOption("--jobs", "-j", takes_value=False, category="selector"),
        ForbiddenOption(
            "--output-format",
            None,
            takes_value=True,
            category="output",
        ),
        ForbiddenOption(
            "--output-into-files",
            None,
            takes_value=False,
            category="output",
        ),
        ForbiddenOption("--quiet", "-q", takes_value=False, category="output"),
    ),
)

_GRAMMARS: typ.Final[dict[str, KaniGrammar]] = {KANI_067.version: KANI_067}

#: Releases the registry can parse, in sorted order.
SUPPORTED_VERSIONS: typ.Final[tuple[str, ...]] = tuple(sorted(_GRAMMARS))


def supported_versions() -> tuple[str, ...]:
    """Return the registered releases in sorted order.

    Returns
    -------
    tuple[str, ...]
        Exact versions the parser and selector are qualified against.
    """
    return SUPPORTED_VERSIONS


def grammar_for(version: str) -> KaniGrammar:
    """Return the registered grammar for one exact release.

    Parameters
    ----------
    version : str
        Exact semantic version named by a Kani banner.

    Returns
    -------
    KaniGrammar
        The registered grammar.

    Raises
    ------
    UnsupportedKaniGrammarError
        Raised when the release has no captured grammar.
    """
    grammar = _GRAMMARS.get(version)
    if grammar is None:
        raise UnsupportedKaniGrammarError(version, SUPPORTED_VERSIONS)
    return grammar
