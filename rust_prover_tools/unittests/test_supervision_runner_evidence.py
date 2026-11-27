"""Hold the supervised run's live evidence and diagnostic references."""

from __future__ import annotations

from rust_prover_tools.supervision import (
    Diagnostics,
    ExecutionStatus,
    Measurement,
    MemorySink,
    NullSink,
    run_supervised,
)
from rust_prover_tools.unittests.supervision_doubles import (
    Script,
    ScriptedCgroup,
    ScriptedLauncher,
    ScriptedProbe,
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
async def test_output_is_streamed_with_identity_and_bounded() -> None:
    """Every line is recorded against its harness as it arrives."""
    clock = VirtualClock()
    jsonl = MemorySink()
    launcher = ScriptedLauncher(
        script=Script(
            lines=(("stdout", "Checking harness harnesses::verified..."),),
            exit_code=0,
        ),
        clock=clock,
    )
    environment = _environment(launcher, clock, sinks=Diagnostics(jsonl=jsonl))

    await run_supervised((spec(),), _config(), environment)

    output = [record for record in jsonl.records if record["record"] == "output"]
    assert output, "no output record was written"
    assert output[0]["package"] == "example"
    assert output[0]["harness"] == "harnesses::verified"
    assert output[0]["stream"] == "stdout"


@_async_test
async def test_the_report_names_each_harnesss_evidence_offset() -> None:
    """A diagnostic reference lets a reader find the harness's own records."""
    clock = VirtualClock()
    launcher = ScriptedLauncher()
    environment = _environment(launcher, clock)

    report = await run_supervised(
        (spec("harnesses::a"), spec("harnesses::b")), _config(), environment
    )

    refs = [result.diagnostic for result in report.results]
    first, second = refs
    assert first is not None
    assert second is not None
    assert first.offset < second.offset


@_async_test
async def test_a_null_sink_produces_no_diagnostic_reference() -> None:
    """A disabled JSONL sink has no offset a reader could follow."""
    clock = VirtualClock()
    launcher = ScriptedLauncher()
    environment = _environment(
        launcher, clock, sinks=Diagnostics(human=NullSink(), jsonl=NullSink())
    )

    report = await run_supervised((spec(),), _config(), environment)

    assert report.results[0].diagnostic is None


@_async_test
async def test_cgroup_values_are_read_before_and_after_each_harness() -> None:
    """Per-harness cgroup evidence names its start and finish values."""
    clock = VirtualClock()
    cgroup = ScriptedCgroup()
    launcher = ScriptedLauncher()
    environment = _environment(launcher, clock, cgroup=cgroup)

    report = await run_supervised((spec(),), _config(), environment)

    observation = report.results[0].cgroup
    assert observation is not None
    assert observation.path == "/user.slice/job.scope"
    assert observation.start is not None
    assert observation.finish is not None
    assert observation.oom_kill_delta == 1


@_async_test
async def test_an_unavailable_cgroup_does_not_stop_the_proof() -> None:
    """A host without cgroup v2 still runs and still reports."""
    clock = VirtualClock()
    cgroup = ScriptedCgroup(path=None)
    launcher = ScriptedLauncher()
    environment = _environment(launcher, clock, cgroup=cgroup)

    report = await run_supervised((spec(),), _config(), environment)

    observation = report.results[0].cgroup
    assert observation is not None
    assert observation.path is None
    assert observation.oom_kill_delta is None
    assert report.results[0].status is ExecutionStatus.PASSED


@_async_test
async def test_an_unavailable_sample_is_never_rendered_as_zero() -> None:
    """A measurement that could not be taken names why."""
    clock = VirtualClock()
    probe = ScriptedProbe(
        rss_values=(Measurement.unavailable("no pid"),),
        available_values=(Measurement.unavailable("meminfo unreadable"),),
    )
    launcher = ScriptedLauncher(script=Script(block=True))
    environment = _environment(launcher, clock, probe=probe)

    report = await run_supervised(
        (spec(),),
        _config(timeout_seconds=1.0, sample_interval_seconds=1.0),
        environment,
    )

    result = report.results[0]
    assert result.peak_group_rss.value is None
    assert result.peak_group_rss.unavailable_reason == "no pid"
    assert result.min_host_available.value is None


def test_the_launcher_bounds_an_oversized_line_with_a_marker() -> None:
    """A line cut at the stream boundary is visibly cut."""
    from rust_prover_tools.supervision.stream_lines import BoundedLineSplitter

    splitter = BoundedLineSplitter(max_line_bytes=64)

    (text,) = splitter.feed(b"x" * 200)

    assert "truncated: line exceeded 64 bytes" in text
