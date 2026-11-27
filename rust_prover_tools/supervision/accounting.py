"""Compare expected and executed harness identities as multisets.

A count is not an identity. Two runs can each execute three harnesses while one
skips a harness and repeats another; an aggregate total would call that
complete. The comparison here is therefore a multiset difference over
package-qualified identities, and it reports each kind of drift separately so a
report can say what is missing, duplicated, or unexpected rather than only that
the totals disagree.

The same comparison serves inventory pin drift, where a committed inventory is
compared with a freshly discovered one, and run accounting, where the expected
inventory is compared with what actually executed.
"""

from __future__ import annotations

import collections
import typing as typ

from rust_prover_tools.supervision.results import InventoryDrift

if typ.TYPE_CHECKING:
    import collections.abc as cabc

__all__ = ("compare_identities", "identity_key")


def identity_key(package: str, harness: str) -> str:
    """Return the stable package-qualified key for one identity.

    Parameters
    ----------
    package : str
        Cargo package name.
    harness : str
        Fully qualified Kani pretty name.

    Returns
    -------
    str
        ``package::harness``, the key every comparison uses.
    """
    return f"{package}::{harness}"


def compare_identities(
    expected: cabc.Iterable[str],
    executed: cabc.Iterable[str],
) -> InventoryDrift:
    """Return the multiset difference between expected and executed identities.

    Parameters
    ----------
    expected : cabc.Iterable[str]
        Package-qualified identities the run should execute.
    executed : cabc.Iterable[str]
        Package-qualified identities the run did execute.

    Returns
    -------
    InventoryDrift
        Missing, duplicated, and unexpected identities, each sorted so two
        reports of the same drift are byte-identical.
    """
    wanted = collections.Counter(expected)
    seen = collections.Counter(executed)
    missing = sorted((wanted - seen).elements())
    unexpected = sorted((seen - wanted).elements())
    # A repeated identity is drift only when it exceeds its expected count, so
    # an exactly matched repeated expectation has no drift.
    duplicate = sorted(
        key for key, count in seen.items() if count > 1 and count > wanted.get(key, 0)
    )
    return InventoryDrift(
        missing=tuple(missing),
        duplicate=tuple(duplicate),
        unexpected=tuple(unexpected),
    )
