"""Shared pytest fixtures for CLI integration tests."""

from __future__ import annotations

import dataclasses as dc
import os
import typing as typ

import pytest

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path


#: mutmut sets this in every process it runs tests in. It is also set for the
#: baseline it runs before scoring any mutant, which is the run whose failure
#: aborts the whole mutation job.
MUTMUT_UNDER_TEST_ENV: typ.Final[str] = "MUTANT_UNDER_TEST"


def is_mutmut_sandbox(environment: cabc.Mapping[str, str]) -> bool:
    """Say whether a test process is running inside mutmut's sandbox.

    Parameters
    ----------
    environment : collections.abc.Mapping of str to str
        The environment to inspect, normally ``os.environ``.

    Returns
    -------
    bool
        ``True`` when mutmut's under-test marker is present.

    """
    return MUTMUT_UNDER_TEST_ENV in environment


#: Whether this pytest process is one mutmut started. Read once because a
#: running process's environment cannot change underneath it, and
#: ``pytest.mark.skipif`` takes a condition rather than a callable in pytest's
#: own type stubs.
IS_MUTMUT_SANDBOX: typ.Final[bool] = is_mutmut_sandbox(os.environ)


@dc.dataclass(frozen=True, slots=True)
class FakeVerusBinarySpec:
    """Configuration for a deterministic fake Verus binary."""

    toolchain: str = "nightly-test"
    version_stdout: str | None = None
    version_exit_code: int = 0
    version_stderr: str = ""
    proof_command: str = "printf 'proof verified\\n'"


@pytest.fixture
def fake_verus_binary_factory() -> cabc.Callable[..., None]:
    """Return a helper that writes deterministic fake Verus binaries."""

    def write_fake_verus_binary(
        path: Path,
        *,
        spec: FakeVerusBinarySpec | None = None,
    ) -> None:
        spec = spec or FakeVerusBinarySpec()
        path.parent.mkdir(parents=True, exist_ok=True)
        version_stdout = spec.version_stdout or (
            f"Verus 0.1.0\\nToolchain: {spec.toolchain}\\n"
        )
        version_output = (
            f"printf '{version_stdout}'"
            if spec.version_exit_code == 0
            else f"printf '{spec.version_stderr}\\n' >&2"
        )
        path.write_text(
            "\n".join(
                [
                    "#!/bin/sh",
                    "set -eu",
                    'if [ "${1:-}" = "--version" ]; then',
                    f"  {version_output}",
                    f"  exit {spec.version_exit_code}",
                    "fi",
                    spec.proof_command,
                ],
            )
            + "\n",
            encoding="utf-8",
        )
        path.chmod(0o755)

    return write_fake_verus_binary
