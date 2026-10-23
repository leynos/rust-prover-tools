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

#: mutmut sets this in every process it runs tests in: ``"fail"`` for the
#: forced-fail check, ``"stats"`` while mapping tests to mutants, and the
#: mutant name while executing one.
MUTMUT_UNDER_TEST_ENV: typ.Final[str] = "MUTANT_UNDER_TEST"


def is_mutmut_running() -> bool:
    """Say whether the current pytest process is running under mutmut."""
    return MUTMUT_UNDER_TEST_ENV in os.environ


#: Apply to the individual tests that call into mutated code after changing
#: the working directory. The marker is evaluated per test at collection, so
#: an ordinary run keeps every test.
SKIP_CHDIR_UNDER_MUTMUT = pytest.mark.skipif(
    is_mutmut_running(),
    reason=(
        "mutmut resolves the project's source_paths against the process "
        "working directory, so a test that chdirs fails inside its sandbox"
    ),
)
