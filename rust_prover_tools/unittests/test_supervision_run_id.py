"""Hold the run-ID seam to the report and JSONL it names.

The identifier is an injected source on `RunPolicyEnvironment`, so a caller
that must label its own run supplies one instead of patching the supervisor.
"""

from __future__ import annotations

from rust_prover_tools.supervision import (
    Diagnostics,
    MemorySink,
    run_supervised,
)
from rust_prover_tools.unittests.supervision_doubles import (
    ScriptedLauncher,
    VirtualClock,
    spec,
)
from rust_prover_tools.unittests.supervision_runner_support import (
    async_test as _async_test,
)
from rust_prover_tools.unittests.supervision_runner_support import (
    config as _config,
)
from rust_prover_tools.unittests.supervision_runner_support import (
    environment as _environment,
)


@_async_test
async def test_the_environment_supplies_the_run_identifier() -> None:
    """A supplied run-ID source names the report and every JSONL record."""
    jsonl = MemorySink()
    clock = VirtualClock()
    environment = _environment(
        ScriptedLauncher(clock=clock),
        clock,
        sinks=Diagnostics(jsonl=jsonl),
        run_id=lambda: "f" * 16,
    )

    report = await run_supervised((spec(),), _config(), environment)

    assert report.run_id == "f" * 16
    assert {record["run_id"] for record in jsonl.records} == {"f" * 16}


@_async_test
async def test_an_explicit_run_identifier_wins_over_the_environment() -> None:
    """A caller-named identifier is not replaced by the environment's source."""
    environment = _environment(
        ScriptedLauncher(),
        VirtualClock(),
        run_id=lambda: "environment",
    )

    report = await run_supervised(
        (spec(),),
        _config(),
        environment,
        run_id="caller-named",
    )

    assert report.run_id == "caller-named"
