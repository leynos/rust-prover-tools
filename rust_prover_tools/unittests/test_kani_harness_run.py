"""Hold the supervised Kani composition root end to end, with doubles."""

from __future__ import annotations

import json
import typing as typ
from pathlib import Path

import pytest

from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_cli_grammar import KANI_067
from rust_prover_tools.kani_harness_run import (
    KaniHarnessDependencies,
    KaniHarnessRun,
    KaniHarnessRunOptions,
    LegacyRoute,
    execute_harnesses,
    prepare_harnesses,
    run_harnesses,
)
from rust_prover_tools.kani_inventory import (
    HarnessInventory,
    InventoryDiscoveryError,
    InventoryHarness,
    PackageInventory,
    compose_arguments,
)
from rust_prover_tools.kani_inventory.discover import cargo_metadata
from rust_prover_tools.kani_routes import PreparedRoute
from rust_prover_tools.supervision import (
    Diagnostics,
    ExecutionStatus,
    LaunchCommand,
    MemorySink,
    SupervisionConfig,
    validate_report_document,
)
from rust_prover_tools.supervision.config import DiagnosticSinks
from rust_prover_tools.unittests.supervision_doubles import (
    Script,
    ScriptedCgroup,
    ScriptedLauncher,
    ScriptedProbe,
    VirtualClock,
    VirtualSleeper,
)

_CORPUS = (
    Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "kani_logs" / "0.67.0"
)


def _harness(pretty: str) -> InventoryHarness:
    """Return one inventory harness."""
    return InventoryHarness(
        package="alpha",
        target="alpha",
        target_kind="lib",
        harness=pretty,
        source="src/lib.rs",
        line=1,
        attributes=("kani::proof",),
    )


def _inventory(*harnesses: str) -> HarnessInventory:
    """Return an inventory of the requested harnesses."""
    return HarnessInventory((
        PackageInventory("alpha", tuple(_harness(name) for name in harnesses)),
    ))


def _log(name: str, pretty: str) -> list[str]:
    """Return one corpus log with its harness name rewritten to `pretty`."""
    import re

    text = (_CORPUS / f"{name}.log").read_text(encoding="utf-8")
    names = re.findall(r"Checking harness (\S+)\.\.\.", text)
    if names:
        text = text.replace(names[0], pretty)
    return text.splitlines()


def _script(name: str, pretty: str) -> Script:
    """Return a scripted child that emits a rewritten corpus log."""
    return Script(
        lines=tuple(("stdout", line) for line in _log(name, pretty)),
        exit_code=0 if name == "verified" else 1,
    )


def _prepared() -> PreparedRoute:
    """Return a prepared route that composes exact selector arguments."""

    def build(harness: InventoryHarness) -> LaunchCommand:
        """Compose one exact selector vector for `harness`."""
        arguments = compose_arguments(harness, KANI_067)
        return LaunchCommand(("driver", "kani", *arguments))

    return PreparedRoute(grammar=KANI_067, kind="test", build=build)


def _test_config(*, sinks: DiagnosticSinks | None = None) -> SupervisionConfig:
    """Return the documented default policy for a scripted child."""
    return SupervisionConfig(
        watchdog_threshold_bytes=None,
        sinks=sinks or DiagnosticSinks(),
    )


# pylint: disable-next=too-many-arguments
def _run(
    tmp_path: Path,
    *,
    inventory: HarnessInventory,
    scripts: dict[str, Script],
    harnesses: tuple[str, ...] = (),
    config: SupervisionConfig | None = None,
) -> tuple[KaniHarnessRun, ScriptedLauncher, MemorySink]:
    """Run the composition root with a scripted launcher."""
    clock = VirtualClock()
    launcher = ScriptedLauncher(per_harness=scripts, clock=clock)
    jsonl = MemorySink()
    dependencies = KaniHarnessDependencies(
        clock=clock,
        launcher=launcher,
        probe=ScriptedProbe(),
        cgroup=ScriptedCgroup(),
        sleeper=VirtualSleeper(clock),
        diagnostics=Diagnostics(jsonl=jsonl),
        prepare=lambda route: _prepared(),
    )
    options = KaniHarnessRunOptions(
        repo_root=tmp_path,
        route=LegacyRoute(installation=tmp_path, cwd=tmp_path),
        harnesses=harnesses,
        inventory=inventory,
        config=config or _test_config(),
        dependencies=dependencies,
    )
    return run_harnesses(options), launcher, jsonl


def test_a_successful_harness_is_passed_and_exits_zero(tmp_path: Path) -> None:
    """Discovery, exact selection, classification, and reporting agree."""
    run, _, _ = _run(
        tmp_path,
        inventory=_inventory("harnesses::a"),
        scripts={"alpha::harnesses::a": _script("verified", "harnesses::a")},
    )

    assert run.report.passed
    assert run.report.results[0].status is ExecutionStatus.PASSED
    assert run.exit_status == 0
    assert "harnesses::a" in "\n".join(run.table)


def test_a_counterexample_fails_the_run(tmp_path: Path) -> None:
    """A proof failure is an ordinary failing result, not a tool error."""
    run, _, _ = _run(
        tmp_path,
        inventory=_inventory("harnesses::a"),
        scripts={"alpha::harnesses::a": _script("counterexample", "harnesses::a")},
    )

    assert run.report.results[0].status is ExecutionStatus.FAILED
    assert not run.report.passed
    assert run.exit_status == 1


def test_a_wrong_selected_identity_is_no_match(tmp_path: Path) -> None:
    """The run cannot succeed by proving a different harness."""
    run, _, _ = _run(
        tmp_path,
        inventory=_inventory("harnesses::a"),
        scripts={"alpha::harnesses::a": _script("verified", "harnesses::other")},
    )

    assert run.report.results[0].status is ExecutionStatus.NO_MATCH
    assert run.exit_status == 1


def test_the_composed_command_carries_the_exact_selector(tmp_path: Path) -> None:
    """The route builder receives exactly the supported selector arguments."""
    _, launcher, _ = _run(
        tmp_path,
        inventory=_inventory("harnesses::a"),
        scripts={"alpha::harnesses::a": _script("verified", "harnesses::a")},
    )

    argv = launcher.commands[0].argv
    assert argv[:4] == ("driver", "kani", "--package", "alpha")
    assert "--harness" in argv
    assert "harnesses::a" in argv
    assert "--exact" in argv
    assert "--output-format=regular" in argv


def test_only_the_selected_harnesses_are_expected(tmp_path: Path) -> None:
    """A harness filter narrows the expected inventory before execution."""
    run, launcher, _ = _run(
        tmp_path,
        inventory=_inventory("harnesses::a", "harnesses::b"),
        scripts={
            "alpha::harnesses::a": _script("verified", "harnesses::a"),
            "alpha::harnesses::b": _script("verified", "harnesses::b"),
        },
        harnesses=("harnesses::b",),
    )

    assert [result.spec.harness for result in run.report.results] == ["harnesses::b"]
    assert len(launcher.commands) == 1


def test_the_final_report_is_written_and_valid(tmp_path: Path) -> None:
    """The configured report path receives the versioned document."""
    report_path = tmp_path / "report.json"
    config = _test_config(sinks=DiagnosticSinks(report_path=report_path))

    _run(
        tmp_path,
        inventory=_inventory("harnesses::a"),
        scripts={"alpha::harnesses::a": _script("verified", "harnesses::a")},
        config=config,
    )

    document = json.loads(report_path.read_text(encoding="utf-8"))
    validate_report_document(document)
    assert document["passed"] is True


def test_an_interrupted_run_is_not_passed(tmp_path: Path) -> None:
    """A run whose signal account is present cannot report success."""
    from rust_prover_tools.unittests.supervision_doubles import StepInterrupts

    clock = VirtualClock()
    launcher = ScriptedLauncher(
        per_harness={"alpha::harnesses::a": Script(block=True)},
        clock=clock,
    )
    interrupts = StepInterrupts()
    interrupts.fire(15)
    dependencies = KaniHarnessDependencies(
        clock=clock,
        launcher=launcher,
        probe=ScriptedProbe(),
        cgroup=ScriptedCgroup(),
        sleeper=VirtualSleeper(clock),
        diagnostics=Diagnostics(jsonl=MemorySink()),
        interrupts=interrupts,
        prepare=lambda route: _prepared(),
    )
    run = run_harnesses(
        KaniHarnessRunOptions(
            repo_root=tmp_path,
            route=LegacyRoute(installation=tmp_path, cwd=tmp_path),
            inventory=_inventory("harnesses::a", "harnesses::b"),
            dependencies=dependencies,
        )
    )

    assert run.report.interrupted
    assert run.report.interrupt_signal == 15
    assert not run.report.passed
    assert run.exit_status == 128 + 15


def test_a_missing_inventory_package_is_refused(tmp_path: Path) -> None:
    """A package filter that the inventory lacks is an error."""
    from rust_prover_tools.kani_inventory import InventoryDiscoveryError

    with pytest.raises(InventoryDiscoveryError, match="does not contain"):
        run_harnesses(
            KaniHarnessRunOptions(
                repo_root=tmp_path,
                route=LegacyRoute(installation=tmp_path, cwd=tmp_path),
                packages=("beta",),
                inventory=_inventory("harnesses::a"),
                dependencies=KaniHarnessDependencies(prepare=lambda route: _prepared()),
            )
        )


def test_the_run_exposes_the_inventory_it_used(tmp_path: Path) -> None:
    """The result names the inventory and the selected harnesses."""
    inventory = _inventory("harnesses::a")
    run, _, _ = _run(
        tmp_path,
        inventory=inventory,
        scripts={"alpha::harnesses::a": _script("verified", "harnesses::a")},
    )

    assert run.inventory == inventory
    assert [harness.harness for harness in run.selected] == ["harnesses::a"]
    assert run.backend_version == "0.67.0"
    assert run.route_kind == "test"


def test_the_diagnostics_are_not_closed_by_the_runner(tmp_path: Path) -> None:
    """A caller-supplied sink remains usable after the run."""
    jsonl = MemorySink()
    clock = VirtualClock()
    dependencies = KaniHarnessDependencies(
        clock=clock,
        launcher=ScriptedLauncher(
            per_harness={"alpha::harnesses::a": _script("verified", "harnesses::a")},
            clock=clock,
        ),
        probe=ScriptedProbe(),
        cgroup=ScriptedCgroup(),
        sleeper=VirtualSleeper(clock),
        diagnostics=Diagnostics(jsonl=jsonl),
        prepare=lambda route: _prepared(),
    )
    run_harnesses(
        KaniHarnessRunOptions(
            repo_root=tmp_path,
            route=LegacyRoute(installation=tmp_path, cwd=tmp_path),
            inventory=_inventory("harnesses::a"),
            dependencies=dependencies,
        )
    )

    jsonl.write({"record": "run-finish", "run_id": "after"})
    assert jsonl.records[-1]["run_id"] == "after"


def test_the_report_document_names_the_measurement_limits(tmp_path: Path) -> None:
    """The written report carries the sampling and scope notes."""
    report_path = tmp_path / "report.json"
    _run(
        tmp_path,
        inventory=_inventory("harnesses::a"),
        scripts={"alpha::harnesses::a": _script("verified", "harnesses::a")},
        config=_test_config(sinks=DiagnosticSinks(report_path=report_path)),
    )

    document = json.loads(report_path.read_text(encoding="utf-8"))
    assert any("sampled" in note or "sample" in note for note in document["notes"])


def test_preparation_opens_no_evidence_file(tmp_path: Path) -> None:
    """Discovery and qualification leave both destinations untouched."""
    human = tmp_path / "human.log"
    jsonl = tmp_path / "evidence.jsonl"
    clock = VirtualClock()
    launcher = ScriptedLauncher(
        per_harness={
            "alpha::harnesses::a": _script("verified", "harnesses::a"),
        },
        clock=clock,
    )
    dependencies = KaniHarnessDependencies(
        clock=clock,
        launcher=launcher,
        probe=ScriptedProbe(),
        cgroup=ScriptedCgroup(),
        sleeper=VirtualSleeper(clock),
        prepare=lambda route: _prepared(),
    )
    config = SupervisionConfig(
        watchdog_threshold_bytes=None,
        sinks=DiagnosticSinks(human_path=human, jsonl_path=jsonl),
    )
    options = KaniHarnessRunOptions(
        repo_root=tmp_path,
        route=LegacyRoute(installation=tmp_path, cwd=tmp_path),
        inventory=_inventory("harnesses::a"),
        config=config,
        dependencies=dependencies,
    )

    preparation = prepare_harnesses(options)

    assert not human.exists()
    assert not jsonl.exists()
    assert [harness.harness for harness in preparation.selected] == ["harnesses::a"]
    assert preparation.commands["alpha::harnesses::a"].argv[:2] == (
        "driver",
        "kani",
    )

    run = execute_harnesses(preparation, config, dependencies)

    assert run.report.passed
    assert human.exists()
    assert jsonl.exists()


def test_a_route_outside_the_repository_root_is_refused(tmp_path: Path) -> None:
    """Discovery and execution must use one workspace, not two."""
    repository = tmp_path / "repository"
    repository.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    dependencies = KaniHarnessDependencies(prepare=lambda route: _prepared())
    options = KaniHarnessRunOptions(
        repo_root=repository,
        route=LegacyRoute(installation=elsewhere, cwd=elsewhere),
        inventory=_inventory("harnesses::a"),
        dependencies=dependencies,
    )

    with pytest.raises(ProverToolError, match="repository root"):
        prepare_harnesses(options)


def test_the_plan_prescribed_entry_point_is_public() -> None:
    """The approved plan names `run_kani_harnesses`; it must be importable."""
    import rust_prover_tools

    assert callable(rust_prover_tools.run_kani_harnesses)
    assert "run_kani_harnesses" in rust_prover_tools.__all__
    assert rust_prover_tools.run_kani_harnesses is not rust_prover_tools.run_harnesses


def test_the_configured_line_limit_reaches_the_launcher() -> None:
    """The run policy's line bound is the launcher's bound, not a default."""
    from rust_prover_tools.kani_harness_models import KaniHarnessPreparation
    from rust_prover_tools.kani_harness_run import _environment

    preparation = KaniHarnessPreparation(
        inventory=_inventory("harnesses::a"),
        selected=(),
        prepared=_prepared(),
        commands={},
    )
    environment = _environment(
        preparation,
        KaniHarnessDependencies(),
        Diagnostics(jsonl=MemorySink()),
        limits=(64, 1024),
    )

    launcher = typ.cast("typ.Any", environment.launcher)
    assert launcher.max_line_bytes == 64


def test_the_log_budget_reaches_the_policy() -> None:
    """A non-default retained-log budget is part of the run policy."""
    from rust_prover_tools.kani_harness_cli_options import (
        DestinationFlags,
        PolicyFlags,
        supervision_policy,
    )

    config = supervision_policy(
        PolicyFlags(max_log_mib=2),
        DestinationFlags(),
    )

    assert config.max_log_bytes == 2 * 1024 * 1024


_FIXTURE_WORKSPACE = (
    Path(__file__).resolve().parents[2]
    / "tests"
    / "fixtures"
    / "kani_harness_workspace"
)


def test_a_stale_gated_test_pin_is_refused_before_qualification() -> None:
    """Preparation refuses the pin on the supported path, before any launch.

    A cfg-disabled test harness can be absent from the build while a
    same-named harness elsewhere is selected. The guard must therefore reject
    the stale pin during preparation, not rely on a run never starting, so the
    route is never qualified for it.
    """
    pinned = InventoryHarness(
        package="beta",
        target="integration",
        target_kind="test",
        harness="gated_proof",
        source="tests/integration.rs",
        line=8,
    )
    inventory = HarnessInventory((PackageInventory("beta", (pinned,)),))
    metadata = cargo_metadata(_FIXTURE_WORKSPACE)
    qualified: list[object] = []

    def prepare(route: object) -> PreparedRoute:
        """Record a qualification the guard should have prevented."""
        qualified.append(route)
        return _prepared()

    options = KaniHarnessRunOptions(
        repo_root=_FIXTURE_WORKSPACE,
        route=LegacyRoute(
            installation=_FIXTURE_WORKSPACE,
            cwd=_FIXTURE_WORKSPACE,
        ),
        inventory=inventory,
        metadata=metadata,
        dependencies=KaniHarnessDependencies(prepare=prepare),
    )

    with pytest.raises(InventoryDiscoveryError, match="cfg gate"):
        prepare_harnesses(options)
    assert qualified == []


@pytest.mark.parametrize("same_name_first", [True, False])
def test_preparation_keys_required_features_by_target_kind(
    *, same_name_first: bool
) -> None:
    """Preparation refuses the target's features whatever metadata order holds.

    Cargo permits a library and a test target with one name. Indexing metadata
    by name alone lets a later library entry answer for the test target and
    hide its ``required-features``, so both orders are exercised through the
    supported preparation path.
    """
    from rust_prover_tools.kani_inventory.discover import (
        CargoPackage,
        CargoTarget,
    )

    pinned = InventoryHarness(
        package="beta",
        target="integration",
        target_kind="test",
        harness="integration_proof",
        source="tests/integration.rs",
        line=4,
    )
    test = CargoTarget(
        name="integration",
        kind="test",
        src_path=_FIXTURE_WORKSPACE / "beta" / "tests" / "integration.rs",
        required_features=("gated",),
    )
    library = CargoTarget(
        name="integration",
        kind="lib",
        src_path=_FIXTURE_WORKSPACE / "beta" / "src" / "helper.rs",
    )
    ordered = (test, library) if same_name_first else (library, test)
    metadata = tuple(
        CargoPackage(
            name=package.name,
            manifest_path=package.manifest_path,
            targets=tuple(target for target in package.targets if target.kind != "test")
            + ordered,
        )
        if package.name == "beta"
        else package
        for package in cargo_metadata(_FIXTURE_WORKSPACE)
    )
    qualified: list[object] = []

    def prepare(route: object) -> PreparedRoute:
        """Record a qualification the guard should have prevented."""
        qualified.append(route)
        return _prepared()

    options = KaniHarnessRunOptions(
        repo_root=_FIXTURE_WORKSPACE,
        route=LegacyRoute(
            installation=_FIXTURE_WORKSPACE,
            cwd=_FIXTURE_WORKSPACE,
        ),
        inventory=HarnessInventory((PackageInventory("beta", (pinned,)),)),
        metadata=metadata,
        dependencies=KaniHarnessDependencies(prepare=prepare),
    )

    with pytest.raises(InventoryDiscoveryError, match="requires features"):
        prepare_harnesses(options)
    assert qualified == []
