"""Run a sequence of harnesses under supervision and report what happened.

This is the public module the approved plan names for the supervised entry
points. The orchestration that decides what a run means lives in
``run_orchestrator``; the two functions here are the asynchronous and
synchronous ways to start it.

The run identifier comes from ``RunEnvironment.policy.run_id`` unless the
caller names one, so a consumer that must label its own runs supplies a source
on the policy value instead of patching the supervisor.
"""

from __future__ import annotations

import asyncio
import typing as typ

from rust_prover_tools.supervision.run_orchestrator import (
    SupervisedRun,
    new_run_id,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from rust_prover_tools.supervision.config import SupervisionConfig
    from rust_prover_tools.supervision.models import HarnessSpec
    from rust_prover_tools.supervision.results import RunReport
    from rust_prover_tools.supervision.run_environment import RunEnvironment

__all__ = (
    "SupervisedRun",
    "new_run_id",
    "run_supervised",
    "run_supervised_sync",
)


async def run_supervised(
    specs: cabc.Sequence[HarnessSpec],
    config: SupervisionConfig,
    environment: RunEnvironment,
    *,
    run_id: str | None = None,
) -> RunReport:
    """Run every expected harness under supervision.

    Parameters
    ----------
    specs : cabc.Sequence[HarnessSpec]
        Identities the run must execute, in selection order.
    config : SupervisionConfig
        Per-harness policy.
    environment : RunEnvironment
        The execution, evidence, and policy values the run reads.
    run_id : str | None
        Run identifier, or `None` to take one from
        `environment.policy.run_id`.

    Returns
    -------
    RunReport
        The typed report with one result per expected identity.
    """
    run = SupervisedRun(
        expected=tuple(specs),
        config=config,
        environment=environment,
        run_id=run_id if run_id is not None else environment.policy.run_id(),
    )
    return await run.execute()


def run_supervised_sync(
    specs: cabc.Sequence[HarnessSpec],
    config: SupervisionConfig,
    environment: RunEnvironment,
    *,
    run_id: str | None = None,
) -> RunReport:
    """Run every expected harness under supervision, from synchronous code.

    Parameters
    ----------
    specs : cabc.Sequence[HarnessSpec]
        Identities the run must execute, in selection order.
    config : SupervisionConfig
        Per-harness policy.
    environment : RunEnvironment
        The execution, evidence, and policy values the run reads.
    run_id : str | None
        Run identifier, or `None` to take one from
        `environment.policy.run_id`.

    Returns
    -------
    RunReport
        The typed report with one result per expected identity.

    Raises
    ------
    RuntimeError
        Raised when called from inside a running event loop, where it cannot
        drive one of its own. An async caller must await `run_supervised`.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(run_supervised(specs, config, environment, run_id=run_id))
    msg = (
        "run_supervised_sync cannot run inside an event loop; await "
        "run_supervised instead"
    )
    raise RuntimeError(msg)
