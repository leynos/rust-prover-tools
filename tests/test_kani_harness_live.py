"""Opt-in live Kani 0.67.0 proof of the supervised fixture workspace.

The deterministic suite cannot show that the real driver selects the exact
harness or that a real counterexample is classified correctly. This test does,
but it needs an installed qualified release and a working Kani toolchain, so it
runs only when `RPT_LIVE_KANI=1` is set. It is not part of the default gate.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from rust_prover_tools.kani_harness_run import (
    KaniHarnessRunOptions,
    LegacyRoute,
    run_harnesses,
)
from rust_prover_tools.supervision import ExecutionStatus, SupervisionConfig

_RELEASE = Path.home() / ".kani" / "kani-0.67.0"
_FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "tests"
    / "fixtures"
    / "kani_harness_workspace"
)


@pytest.mark.skipif(
    os.environ.get("RPT_LIVE_KANI") != "1" or not _RELEASE.is_dir(),
    reason="set RPT_LIVE_KANI=1 and install Kani 0.67.0 to run the live fixture",
)
@pytest.mark.timeout(900)
def test_a_live_run_classifies_passing_path_and_failing_proofs() -> None:
    """The real driver verifies two proofs and refutes one, exactly once each."""
    run = run_harnesses(
        KaniHarnessRunOptions(
            repo_root=_FIXTURE,
            route=LegacyRoute(installation=_RELEASE, cwd=_FIXTURE),
            packages=("alpha",),
            harnesses=(
                "harnesses::verified",
                "path_proofs::via_path",
                "harnesses::failing",
            ),
            config=SupervisionConfig(
                timeout_seconds=300.0,
                sample_interval_seconds=1.0,
                heartbeat_interval_seconds=5.0,
                watchdog_threshold_bytes=None,
            ),
        )
    )

    statuses = {result.spec.harness: result.status for result in run.report.results}
    assert statuses["harnesses::verified"] is ExecutionStatus.PASSED
    assert statuses["path_proofs::via_path"] is ExecutionStatus.PASSED
    assert statuses["harnesses::failing"] is ExecutionStatus.FAILED
    assert run.report.is_complete
    assert run.exit_status == 1


def test_the_live_fixture_exists() -> None:
    """The opt-in case names a fixture this checkout contains."""
    assert _FIXTURE.is_dir()
