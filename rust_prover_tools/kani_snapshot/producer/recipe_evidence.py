"""The pinned upstream tree evidence a recipe document is checked against.

`validate_recipe` is the only gate a recipe document passes through, so a rule
that a document must agree with the upstream release tree needs that tree's
gitlinks available to the domain. They are recorded here as plain data rather
than read at validation time, because validation decides whether a document may
exist at all: it cannot itself depend on the network, a checkout, or a fixture
that a caller might not have.

`pinned_recipe` builds its submodule records from this tuple, so the recipe and
the rule that checks it cannot drift apart.
"""

from __future__ import annotations

import typing as typ

__all__ = ("PINNED_SUBMODULES", "PINNED_UPSTREAM_COMMIT")

#: The upstream commit the released `kani-0.68.0` tag points at.
PINNED_UPSTREAM_COMMIT: typ.Final[str] = "0d2328a93f0e0ff66132d6bfa1a7d884877cf862"

#: Path and commit of every gitlink in the pinned release's tree, by path.
PINNED_SUBMODULES: typ.Final[tuple[tuple[str, str], ...]] = (
    ("charon", "b250680abd40ff1aaa07081d0497dc2755ed112e"),
    ("firecracker", "cd36c699f3cb3d531289aadee26c45c1306edcfc"),
    ("tests/perf/s2n-quic", "a33d369b98a50fd9290fd6586832c969035bdec0"),
)
