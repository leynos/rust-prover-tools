"""Shared pytest fixtures for CLI integration tests."""

from __future__ import annotations

import dataclasses as dc
import typing as typ

import pytest

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path


@dc.dataclass(frozen=True, slots=True)
class FakeVerusBinarySpec:
    """Configuration for a deterministic fake Verus binary."""

    toolchain: str = "nightly-test"
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
        version_output = (
            f"printf 'Verus 0.1.0\\nToolchain: {spec.toolchain}\\n'"
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
