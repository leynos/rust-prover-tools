"""Bounded exhaustive properties of the pure readings.

Each property enumerates every input in a small, fully specified domain
rather than sampling a large one: every directed call graph over three
workflows, every conjunction of up to three terms under each separator
spelling, and every document shape up to three levels deep. Within its
bound each is a proof by enumeration, and the bounds are chosen so that
the cases the rules exist for (chains, cycles, self-calls, quoted
operators, a key or a value at any depth) all fall inside them.
"""

from __future__ import annotations

import functools
import itertools
import typing as typ

import pytest

from .codescene_reach import pull_request_closure
from .expressions import ConditionError, conjuncts, missing_terms
from .fixtures import REPOSITORY
from .loading import WorkflowReadingError
from .reading import texts

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from .loading import Document

NODES: typ.Final[tuple[int, ...]] = (0, 1, 2)
EDGES: typ.Final[tuple[tuple[int, int], ...]] = tuple(itertools.product(NODES, NODES))
TERMS: typ.Final[tuple[str, ...]] = (
    "a",
    "b == 'x'",
    "env.T != ''",
    "c == 'p && q'",
    "d == 'r || s'",
    "(e || f)",
    "!(g && h)",
)
SEPARATORS: typ.Final[tuple[str, ...]] = ("&&", " && ", "  &&\t", "\n&&  ")
MARKER: typ.Final[str] = "planted-marker"


def _name(node: int) -> str:
    """Return the workflow file name for one graph node."""
    return f"w{node}.yml"


def _workflow(node: int, callees: list[int], *, is_seed: bool) -> Document:
    """Return a workflow calling each callee, alternating the local spellings."""
    spellings = ("./", "$/")
    calls = {
        f"call{callee}": {
            "uses": f"{spellings[(node + callee) % 2]}.github/workflows/{_name(callee)}"
        }
        for callee in callees
    }
    return {
        "on": "pull_request" if is_seed else {"workflow_call": None},
        "jobs": calls or {"noop": {"runs-on": "x", "steps": []}},
    }


def _reachable(edges: set[tuple[int, int]], seeds: set[int]) -> set[int]:
    """Return the nodes reachable from the seeds, by Warshall's closure."""
    reach = {(i, j) for i, j in EDGES if i == j or (i, j) in edges}
    for k, i, j in itertools.product(NODES, NODES, NODES):
        if (i, k) in reach and (k, j) in reach:
            reach.add((i, j))
    return {j for i, j in reach if i in seeds}


#: Every non-empty set of seed workflows.
SEED_SETS: typ.Final[tuple[frozenset[int], ...]] = tuple(
    frozenset(seeds)
    for size in (1, 2, 3)
    for seeds in itertools.combinations(NODES, size)
)


def _graphs() -> cabc.Iterator[tuple[set[tuple[int, int]], set[int]]]:
    """Yield every edge set over three nodes with every non-empty seed set."""
    for mask, seeds in itertools.product(range(1 << len(EDGES)), SEED_SETS):
        edges = {edge for bit, edge in enumerate(EDGES) if mask >> bit & 1}
        yield edges, set(seeds)


def _documents(edges: set[tuple[int, int]], seeds: set[int]) -> dict[str, Document]:
    """Build the workflow tree one graph describes."""
    return {
        _name(node): _workflow(
            node,
            sorted(j for i, j in edges if i == node),
            is_seed=node in seeds,
        )
        for node in NODES
    }


def test_the_closure_is_reachability_over_every_small_graph() -> None:
    """The closure equals Warshall reachability for all 3,584 graphs and seeds."""
    wrong = [
        (sorted(edges), sorted(seeds))
        for edges, seeds in _graphs()
        if set(pull_request_closure(_documents(edges, seeds), REPOSITORY))
        != {_name(node) for node in _reachable(edges, seeds)}
    ]
    assert not wrong, wrong[:5]


def _is_refused(documents: dict[str, Document]) -> bool:
    """Return whether reading the closure over a tree raises."""
    try:
        pull_request_closure(documents, REPOSITORY)
    except WorkflowReadingError:
        return True
    return False


def _with_bad_reference(
    edges: set[tuple[int, int]], seeds: set[int], reference: str
) -> dict[str, Document]:
    """Build a graph's tree with the last workflow's calls replaced by one reference."""
    documents = _documents(edges, seeds)
    documents[_name(2)]["jobs"] = {"bad": {"uses": reference}}
    return documents


@pytest.mark.parametrize(
    "reference",
    ["./.github/workflows/absent.yml", "leynos/example/.github/workflows/w0.yml@v1"],
)
def test_a_bad_reference_is_refused_exactly_when_reachable(reference: str) -> None:
    """A missing or qualified call raises if and only if a seed reaches it."""
    wrong = [
        (sorted(edges), sorted(seeds))
        for edges, seeds in _graphs()
        if _is_refused(_with_bad_reference(edges, seeds, reference))
        != (2 in _reachable(edges, seeds))
    ]
    assert not wrong, wrong[:5]


def _term_sequences(sizes: tuple[int, ...]) -> cabc.Iterator[tuple[str, ...]]:
    """Yield every sequence of terms of each given length."""
    return itertools.chain.from_iterable(
        itertools.product(TERMS, repeat=size) for size in sizes
    )


def _conditions() -> cabc.Iterator[tuple[str, list[str]]]:
    """Yield every conjunction of one to three terms under every spelling."""
    for terms, separator in itertools.product(_term_sequences((1, 2, 3)), SEPARATORS):
        body = separator.join(terms)
        yield body, list(terms)
        yield f"${{{{ {body} }}}}", list(terms)


def test_a_conjunction_splits_into_its_terms() -> None:
    """Every generated conjunction reads back as its terms, wrapped or not."""
    wrong = [
        condition
        for condition, terms in _conditions()
        if conjuncts(condition) != terms or missing_terms(condition, frozenset(terms))
    ]
    assert not wrong, wrong[:5]


def _disjunctions() -> cabc.Iterator[str]:
    """Yield every conjunction with one of its separators replaced by `||`."""
    for terms, separator in itertools.product(_term_sequences((2, 3)), SEPARATORS):
        for position in range(len(terms) - 1):
            joins = [separator] * (len(terms) - 1)
            joins[position] = " || "
            yield terms[0] + "".join(
                join + term for join, term in zip(joins, terms[1:], strict=True)
            )


def _is_accepted(condition: str) -> bool:
    """Return whether the condition reader accepts a condition."""
    try:
        conjuncts(condition)
    except ConditionError:
        return False
    return True


def test_an_unquoted_disjunction_anywhere_is_refused() -> None:
    """Replacing any one separator with `||` makes the condition refused."""
    accepted = [condition for condition in _disjunctions() if _is_accepted(condition)]
    assert not accepted, accepted[:5]


WRAPPERS: typ.Final[tuple[cabc.Callable[[object], object], ...]] = (
    lambda child: {"k": child},
    lambda child: [child],
    lambda child: {"k": child, "o": "noise"},
    lambda child: ["noise", child, 3],
)


def _placements() -> cabc.Iterator[object]:
    """Yield the marker as a key and as a value under every wrapper chain."""
    leaves: tuple[object, ...] = (MARKER, {MARKER: None}, {MARKER: "v"})
    chains = itertools.chain.from_iterable(
        itertools.product(WRAPPERS, repeat=depth) for depth in range(4)
    )
    for chain, leaf in itertools.product(chains, leaves):
        yield functools.reduce(lambda document, wrap: wrap(document), chain, leaf)


def test_every_key_and_scalar_is_read_at_any_depth() -> None:
    """The marker is read wherever it is placed, as a key or as a value."""
    missed = [
        document for document in _placements() if MARKER not in set(texts(document))
    ]
    assert not missed, missed[:5]
