"""Shared fixtures and guards for the in-package unit tests.

These tests also run inside mutmut's ``mutants/`` sandbox, where mutmut
trampolines every mutable function through
``mutmut.mutation.trampoline``. The trampoline resolves the configured
``source_paths`` against the process working directory on every call, so a
test that changes into a directory without a ``rust_prover_tools`` child
fails inside mutmut while passing in an ordinary run. Such a failure aborts
the baseline mutmut runs before it scores any mutant.
"""

from __future__ import annotations

import os
import typing as typ

import pytest

if typ.TYPE_CHECKING:
    import collections.abc as cabc

#: mutmut sets this in every process it runs tests in: ``"fail"`` for the
#: forced-fail check, ``"stats"`` while mapping tests to mutants, and the
#: mutant name while executing one. It is set before pytest starts, so the
#: value read while this module is imported is the value the whole run sees.
MUTMUT_UNDER_TEST_ENV: typ.Final[str] = "MUTANT_UNDER_TEST"


def reads_mutmut_marker(environment: cabc.Mapping[str, str]) -> bool:
    """Say whether a test process was started by mutmut.

    Parameters
    ----------
    environment : collections.abc.Mapping of str to str
        The environment to inspect, normally ``os.environ``.

    Returns
    -------
    bool
        ``True`` when mutmut's under-test marker is present, ``False`` in an
        ordinary run.

    """
    return MUTMUT_UNDER_TEST_ENV in environment


#: Whether this pytest process is one mutmut started. Read once because the
#: environment of a running process cannot change underneath it, and
#: ``pytest.mark.skipif`` takes a condition rather than a callable in
#: pytest's own type stubs.
IS_MUTMUT_RUN: typ.Final[bool] = reads_mutmut_marker(os.environ)

#: Apply to the individual tests that call into mutated code after changing
#: the working directory. The marker is per test, so an ordinary run keeps
#: every test in the module.
SKIP_CHDIR_UNDER_MUTMUT = pytest.mark.skipif(
    IS_MUTMUT_RUN,
    reason=(
        "mutmut resolves the project's source_paths against the process "
        "working directory, so a test that chdirs fails inside its sandbox"
    ),
)
