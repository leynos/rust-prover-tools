"""Shared doubles for the supervision runner teardown tests."""

from __future__ import annotations

import typing as typ

from rust_prover_tools.supervision import (
    ExecutionStatus,
    HarnessClassification,
    HarnessSpec,
    ResourceSample,
)
from rust_prover_tools.unittests.supervision_runner_support import NullObserver

if typ.TYPE_CHECKING:
    import datetime as dt


class _ProbeFailedError(RuntimeError):
    """The test probe failed while sampling."""

    def __init__(self) -> None:
        """Record the failure."""
        super().__init__("probe failed")


class _ExplodingProbe:
    """A probe whose first sample fails, to prove the monitor surfaces it."""

    def sample(
        self,
        spec: HarnessSpec,
        *,
        group_id: int | None,
        started_at: dt.datetime,
        started_monotonic: float,
    ) -> ResourceSample:
        """Raise instead of measuring."""
        del spec, group_id, started_at, started_monotonic
        raise _ProbeFailedError


class _UnclosableObserver(NullObserver):
    """An observer whose cleanup always fails."""

    def close(self) -> None:
        """Fail as a released handle or a broken spool would."""
        msg = "the spool could not be closed"
        raise RuntimeError(msg)


class _UnclosableAdapter:
    """An adapter that always supplies the failing observer."""

    def observer(self, spec: HarnessSpec) -> _UnclosableObserver:
        """Return the observer whose close raises."""
        del spec
        return _UnclosableObserver()

    def classify(
        self,
        evidence: object,
        observation: object,
    ) -> HarnessClassification:
        """Report the attempt as interrupted; the test is about its close."""
        del evidence, observation
        return HarnessClassification(status=ExecutionStatus.INTERRUPTED)
