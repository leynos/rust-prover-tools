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

from rust_prover_tools import platforms

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

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


@pytest.fixture
def linux_x86_64_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pretend the host is Linux x86_64 so installer tests are portable.

    The prebuilt installers reject every other host before they touch the
    network or the filesystem. Tests that exercise the happy path must not
    inherit a contributor's real host check, or they fail on macOS and on
    Linux arm64. Tests for unsupported hosts override these attributes with
    their own ``monkeypatch`` calls.
    """
    monkeypatch.setattr(platforms.platform, "system", lambda: "Linux")
    monkeypatch.setattr(platforms.platform, "machine", lambda: "x86_64")


@pytest.fixture
def cargo_profile_project(tmp_path: Path) -> cabc.Callable[..., dict[str, str]]:
    """Create a Cargo configuration selecting development backends.

    The returned factory takes the profile names to select. Each profile gets
    the Cranelift backend, which the qualified release does not build with, so
    a child that fails to shadow the selection cannot compile. It also returns
    the ``HOME``/``CARGO_HOME`` pair that keeps Cargo discovery inside the
    test's temporary directory.
    """

    def create(*profiles: str) -> dict[str, str]:
        config = tmp_path / ".cargo"
        config.mkdir(parents=True, exist_ok=True)
        tables = ["[unstable]\ncodegen-backend = true"]
        tables.extend(
            f'[profile.{profile}]\ncodegen-backend = "cranelift"'
            for profile in profiles
        )
        tables.append("")
        (config / "config.toml").write_text("\n".join(tables), encoding="utf-8")
        return {
            "HOME": str(tmp_path),
            "CARGO_HOME": str(tmp_path / "cargo-home"),
        }

    return create
