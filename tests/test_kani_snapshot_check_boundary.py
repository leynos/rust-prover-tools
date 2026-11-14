"""The boundary between the snapshot query and compiler verification.

The query validates installed bytes and starts no process; verification runs
the generation's compilers through the configured admission route. These tests
sit on that seam: a configured wrapper must be untouched by the query, invoked
once per probe command by verification, must receive exactly the environment
its caller injected, and a refusal must surface as a snapshot error.
"""

from __future__ import annotations

import shlex
import typing as typ

import pytest

from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_snapshot.identity import runtime_for, snapshot_layout
from rust_prover_tools.kani_snapshot.install import check_snapshot, install_snapshot
from rust_prover_tools.kani_snapshot.install_defaults import (
    probe_generation,
    probe_host_generation,
)
from rust_prover_tools.kani_snapshot.install_validation import (
    validate_generation,
    verify_effective_compiler,
)
from tests.kani_snapshot_install_fixtures import (
    active_generation,
    install_hooks,
    install_options,
    make_case,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path


def _recording_wrapper(
    tmp_path: Path,
    *,
    runs: str,
) -> tuple[Path, Path]:
    """Write a wrapper that logs each invocation and runs `runs`.

    Returns the wrapper and the log file it appends to. `runs` is the shell
    command the wrapper executes after logging, so one helper models both a
    wrapper that forwards to the compiler and one that fails instead.
    """
    log = tmp_path / "wrapper-invocations.log"
    wrapper = tmp_path / "recording-wrapper"
    wrapper.write_text(
        f"#!/bin/sh\nprintf '%s\\n' \"$*\" >> {shlex.quote(str(log))}\n{runs}\n",
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    return wrapper, log


@pytest.mark.timeout(30)
def test_the_process_free_query_never_runs_a_configured_wrapper(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Checking the installed bytes cannot trigger a compiler wrapper.

    An operator may configure a wrapper with side effects of its own. The query
    is advertised as process-free, so a generation that validates must leave a
    wrapper that records its invocations completely untouched.
    """
    case = make_case(tmp_path)
    install_snapshot(install_options(case), install_hooks(case))
    wrapper, log = _recording_wrapper(tmp_path, runs="exit 1")
    monkeypatch.setenv("RUSTC_WRAPPER", str(wrapper))

    validated = check_snapshot(install_options(case))

    assert validated.manifest == case.manifest, (
        "the query validated a different manifest than the active pin names"
    )
    assert not log.exists(), "the process-free query ran the compiler wrapper"


@pytest.mark.timeout(30)
def test_verification_runs_the_wrapper_once_per_probe_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The effectful half crosses the real process boundary.

    The wrapper is a real executable on the admission chain, so this test
    exercises `verify_effective_compiler` the way an operator's configuration
    reaches it: through `RUSTC_WRAPPER` and the host environment.
    """
    case = make_case(tmp_path)
    install_snapshot(install_options(case), install_hooks(case))
    wrapper, log = _recording_wrapper(tmp_path, runs='exec "$@"')
    monkeypatch.setenv("RUSTC_WRAPPER", str(wrapper))
    layout = snapshot_layout(
        case.pin,
        cache_home=case.cache_home,
        generation=active_generation(case),
    )

    validated = validate_generation(install_options(case), case.manifest, layout)
    identity = verify_effective_compiler(validated, probe_host_generation)

    assert identity == validated.identity, "verification reported another identity"
    invocations = log.read_text(encoding="utf-8").splitlines()
    assert len(invocations) == 2, (
        "verification did not run exactly one wrapper invocation per probe"
    )
    assert invocations[0].endswith("-vV"), "the first probe was not `rustc -vV`"
    assert invocations[1].endswith("--version"), (
        "the second probe was not `kani-compiler --version`"
    )


@pytest.mark.timeout(30)
def test_probe_children_receive_only_the_injected_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The probe's children run in the caller's mapping, not the host's.

    A Cuprum overlay keeps every variable the mapping does not name at its
    live value, so an injected environment could be shadowed by whatever the
    process happened to hold. The wrapper records the environment each child
    actually received, which is the only place the difference is visible.
    """
    case = make_case(tmp_path)
    install_snapshot(install_options(case), install_hooks(case))
    log = tmp_path / "child-environment.log"
    wrapper = tmp_path / "environment-wrapper"
    wrapper.write_text(
        f'#!/bin/sh\n/usr/bin/env >> {shlex.quote(str(log))}\nexec "$@"\n',
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    monkeypatch.setenv("HOST_ONLY_MARKER", "host-value")
    layout = snapshot_layout(
        case.pin,
        cache_home=case.cache_home,
        generation=active_generation(case),
    )
    runtime = runtime_for(layout, case.manifest)

    def admit(_environment: cabc.Mapping[str, str]) -> tuple[str, ...]:
        """Admit the recording wrapper without consulting the host."""
        return (str(wrapper),)

    probe_generation(
        runtime,
        environment={"INJECTED_MARKER": "injected-value"},
        resolve_admission=admit,
    )

    recorded = log.read_text(encoding="utf-8")
    assert "INJECTED_MARKER=injected-value" in recorded, (
        "the probe child did not receive the injected mapping"
    )
    assert "HOST_ONLY_MARKER" not in recorded, (
        "the probe child inherited a variable the caller never supplied"
    )


@pytest.mark.timeout(30)
def test_verification_reports_a_failing_wrapper_as_a_snapshot_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A wrapper that refuses the probe surfaces as a snapshot error."""
    case = make_case(tmp_path)
    install_snapshot(install_options(case), install_hooks(case))
    wrapper, log = _recording_wrapper(tmp_path, runs="exit 9")
    monkeypatch.setenv("RUSTC_WRAPPER", str(wrapper))
    layout = snapshot_layout(
        case.pin,
        cache_home=case.cache_home,
        generation=active_generation(case),
    )
    validated = validate_generation(install_options(case), case.manifest, layout)

    with pytest.raises(ProverToolError):
        verify_effective_compiler(validated, probe_host_generation)

    assert log.exists(), "the failing wrapper was never invoked"
