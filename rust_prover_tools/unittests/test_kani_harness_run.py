"""Hold the supervised Kani composition root end to end, with doubles."""

from __future__ import annotations

import json
import subprocess
import sys
import typing as typ
from pathlib import Path

import pytest

from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_cli_grammar import KANI_067
from rust_prover_tools.kani_harness_run import (
    KaniExecutionDependencies,
    KaniHarnessRun,
    KaniHarnessRunOptions,
    KaniPreparationDependencies,
    LegacyRoute,
    execute_harnesses,
    qualify_harnesses,
    run_harnesses,
)
from rust_prover_tools.kani_inventory import (
    HarnessInventory,
    InventoryDiscoveryError,
    InventoryHarness,
    PackageInventory,
    compose_arguments,
)
from rust_prover_tools.kani_inventory.discover import CargoPackage, cargo_metadata
from rust_prover_tools.kani_routes import PreparedRoute
from rust_prover_tools.supervision import (
    Diagnostics,
    ExecutionStatus,
    LaunchCommand,
    MemorySink,
    SupervisionConfig,
    render_table,
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
    unwritten,
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


def _prepared_dependencies() -> KaniPreparationDependencies:
    """Return preparation seams that qualify without a real installation."""
    return KaniPreparationDependencies(prepare=lambda route: _prepared())


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
    dependencies = KaniExecutionDependencies(
        clock=clock,
        launcher=launcher,
        probe=ScriptedProbe(),
        cgroup=ScriptedCgroup(),
        sleeper=VirtualSleeper(clock),
        diagnostics=Diagnostics(jsonl=jsonl),
    )
    options = KaniHarnessRunOptions(
        repo_root=tmp_path,
        route=LegacyRoute(installation=tmp_path, cwd=tmp_path),
        harnesses=harnesses,
        inventory=inventory,
    )
    return (
        run_harnesses(
            options,
            config or _test_config(),
            dependencies,
            _prepared_dependencies(),
        ),
        launcher,
        jsonl,
    )


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
    assert "harnesses::a" in "\n".join(render_table(run.report))


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
    dependencies = KaniExecutionDependencies(
        clock=clock,
        launcher=launcher,
        probe=ScriptedProbe(),
        cgroup=ScriptedCgroup(),
        sleeper=VirtualSleeper(clock),
        diagnostics=Diagnostics(jsonl=MemorySink()),
        interrupts=interrupts,
    )
    run = run_harnesses(
        KaniHarnessRunOptions(
            repo_root=tmp_path,
            route=LegacyRoute(installation=tmp_path, cwd=tmp_path),
            inventory=_inventory("harnesses::a", "harnesses::b"),
        ),
        _test_config(),
        dependencies,
        _prepared_dependencies(),
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
            ),
            _test_config(),
            preparation_dependencies=_prepared_dependencies(),
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
    dependencies = KaniExecutionDependencies(
        clock=clock,
        launcher=ScriptedLauncher(
            per_harness={"alpha::harnesses::a": _script("verified", "harnesses::a")},
            clock=clock,
        ),
        probe=ScriptedProbe(),
        cgroup=ScriptedCgroup(),
        sleeper=VirtualSleeper(clock),
        diagnostics=Diagnostics(jsonl=jsonl),
    )
    run_harnesses(
        KaniHarnessRunOptions(
            repo_root=tmp_path,
            route=LegacyRoute(installation=tmp_path, cwd=tmp_path),
            inventory=_inventory("harnesses::a"),
        ),
        _test_config(),
        dependencies,
        _prepared_dependencies(),
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


def test_preparation_qualifies_with_the_checked_working_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The directory the run checked is the one qualification receives."""
    from rust_prover_tools import kani_harness_run as run_module

    seen: list[Path | None] = []

    def recording(
        route: object,
        *,
        environ: object,
        cwd: Path,
    ) -> PreparedRoute:
        """Record the working directory qualification was given."""
        del route, environ
        seen.append(cwd)
        return _prepared()

    monkeypatch.setattr(run_module, "prepare_route", recording)
    options = KaniHarnessRunOptions(
        repo_root=tmp_path,
        route=LegacyRoute(installation=tmp_path, cwd=tmp_path),
        inventory=_inventory("harnesses::a"),
    )

    qualify_harnesses(options)

    assert seen == [tmp_path.resolve()]


class _PathSeenError(Exception):
    """Stop preparation once qualification has recorded its directory."""


@pytest.mark.parametrize("snapshot", [False, True])
def test_preparation_qualifies_at_the_checked_directory_for_both_routes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    snapshot: bool,
) -> None:
    """A relative route directory resolves to the checked absolute directory."""
    from rust_prover_tools.kani_routes import SnapshotRoute

    seen: list[Path] = []

    def record(context: Path) -> None:
        """Record the directory qualification received."""
        seen.append(context)
        raise _PathSeenError

    def legacy_qualification(options: object, environment: object) -> object:
        """Record the legacy working directory."""
        del environment
        record(typ.cast("typ.Any", options).cwd)
        return object()

    def snapshot_qualification(
        options: object,
        hooks: object = None,
        *,
        context: object,
        validate_arguments: bool = True,
    ) -> object:
        """Record the snapshot working directory."""
        del options, hooks, validate_arguments
        record(typ.cast("typ.Any", context).cwd)
        return object()

    repo_root = tmp_path / "nested" / "project"
    repo_root.mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "rust_prover_tools.kani_routes.qualify_kani_installation",
        legacy_qualification,
    )
    monkeypatch.setattr(
        "rust_prover_tools.kani_routes.prepare_snapshot_proof",
        snapshot_qualification,
    )
    route = (
        SnapshotRoute(
            pin=typ.cast("typ.Any", object()),
            cwd=Path("nested", "project"),
        )
        if snapshot
        else LegacyRoute(
            installation=repo_root,
            cwd=Path("nested", "project"),
        )
    )
    options = KaniHarnessRunOptions(
        repo_root=repo_root,
        route=route,
        inventory=_inventory("harnesses::a"),
    )

    with pytest.raises(_PathSeenError):
        qualify_harnesses(options)

    assert seen == [repo_root.resolve()]


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
    dependencies = KaniExecutionDependencies(
        clock=clock,
        launcher=launcher,
        probe=ScriptedProbe(),
        cgroup=ScriptedCgroup(),
        sleeper=VirtualSleeper(clock),
    )
    config = SupervisionConfig(
        watchdog_threshold_bytes=None,
        sinks=DiagnosticSinks(human_path=human, jsonl_path=jsonl),
    )
    options = KaniHarnessRunOptions(
        repo_root=tmp_path,
        route=LegacyRoute(installation=tmp_path, cwd=tmp_path),
        inventory=_inventory("harnesses::a"),
    )

    preparation = qualify_harnesses(options, _prepared_dependencies())

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


def test_preparation_consumes_only_injected_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Injected values reach preparation without a process-directory read."""
    from rust_prover_tools.kani_harness_run import _environment
    from rust_prover_tools.kani_routes import SnapshotRoute

    def refuse() -> Path:
        """Fail if preparation consults the process directory."""
        msg = "preparation read the process directory"
        raise AssertionError(msg)

    monkeypatch.setattr(Path, "cwd", refuse)
    prepared = _prepared()
    adapter = typ.cast("typ.Any", object())
    clock = VirtualClock()
    human = tmp_path / "human.log"
    jsonl = tmp_path / "evidence.jsonl"
    dependencies = KaniExecutionDependencies(
        clock=clock,
        launcher=ScriptedLauncher(clock=clock),
        probe=ScriptedProbe(),
        cgroup=ScriptedCgroup(),
        sleeper=VirtualSleeper(clock),
        adapter=adapter,
    )
    config = _test_config(sinks=DiagnosticSinks(human_path=human, jsonl_path=jsonl))
    options = KaniHarnessRunOptions(
        repo_root=tmp_path,
        route=SnapshotRoute(pin=typ.cast("typ.Any", object())),
        inventory=_inventory("harnesses::a"),
    )
    preparation_dependencies = KaniPreparationDependencies(
        prepare=lambda route: prepared,
        process_cwd=tmp_path,
        metadata=lambda root: (),
    )

    preparation = qualify_harnesses(options, preparation_dependencies)

    assert preparation.prepared is prepared
    assert preparation.commands["alpha::harnesses::a"].argv[:2] == (
        "driver",
        "kani",
    )
    assert unwritten(human), "preparation wrote the human log"
    assert unwritten(jsonl), "preparation wrote the JSONL evidence"
    environment = _environment(
        preparation,
        dependencies,
        Diagnostics(),
        limits=(config.max_line_bytes, config.max_log_bytes),
    )
    assert environment.execution.adapter is adapter


def test_the_injected_process_directory_is_the_one_checked(
    tmp_path: Path,
) -> None:
    """A snapshot route naming none uses the supplied cwd, not the process one."""
    from rust_prover_tools.kani_routes import SnapshotRoute

    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    dependencies = KaniPreparationDependencies(
        prepare=lambda route: _prepared(),
        process_cwd=elsewhere,
    )
    options = KaniHarnessRunOptions(
        repo_root=tmp_path,
        route=SnapshotRoute(pin=typ.cast("typ.Any", object())),
        inventory=_inventory("harnesses::a"),
    )

    with pytest.raises(ProverToolError, match="repository root"):
        qualify_harnesses(options, dependencies)


def test_a_route_outside_the_repository_root_is_refused(tmp_path: Path) -> None:
    """Discovery and execution must use one workspace, not two."""
    repository = tmp_path / "repository"
    repository.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    dependencies = KaniPreparationDependencies(prepare=lambda route: _prepared())
    options = KaniHarnessRunOptions(
        repo_root=repository,
        route=LegacyRoute(installation=elsewhere, cwd=elsewhere),
        inventory=_inventory("harnesses::a"),
    )

    with pytest.raises(ProverToolError, match="repository root"):
        qualify_harnesses(options, dependencies)


def test_the_plan_prescribed_entry_point_is_public() -> None:
    """The plan names `run_kani_harnesses`; it stays an importable alias."""
    import rust_prover_tools

    assert callable(rust_prover_tools.run_kani_harnesses)
    assert "run_kani_harnesses" in rust_prover_tools.__all__
    assert rust_prover_tools.run_kani_harnesses is rust_prover_tools.run_harnesses


def test_importing_the_package_does_not_run_the_grammar_check() -> None:
    """Package import must stay light: the Kani names are lazy.

    The grammar registry runs an import-time alignment check, so importing it
    eagerly would make a plain ``import rust_prover_tools`` execute work the
    caller did not ask for. A fresh interpreter keeps this assertion
    independent of whatever the test session already imported.
    """
    script = (
        "import sys, rust_prover_tools;"
        "print('rust_prover_tools.kani_cli_grammar' in sys.modules)"
    )
    done = subprocess.run(
        [sys.executable, "-I", "-c", script],
        capture_output=True,
        text=True,
        check=False,
    )

    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "False", done.stdout


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
        KaniExecutionDependencies(),
        Diagnostics(jsonl=MemorySink()),
        limits=(64, 1024),
    )

    launcher = typ.cast("typ.Any", environment.execution.launcher)
    assert launcher.max_line_bytes == 64


def test_the_log_budget_reaches_the_policy() -> None:
    """A non-default retained-log budget is part of the run policy."""
    from rust_prover_tools.kani_destinations import supervision_policy
    from rust_prover_tools.kani_harness_cli_options import (
        DestinationFlags,
        PolicyFlags,
    )

    config = supervision_policy(
        PolicyFlags(max_log_mib=2),
        DestinationFlags(),
    )

    assert config.max_log_bytes == 2 * 1024 * 1024


def test_the_watchdog_floor_reaches_the_policy_in_bytes() -> None:
    """An enabled watchdog maps its MiB floor to a byte threshold."""
    from rust_prover_tools.kani_destinations import supervision_policy
    from rust_prover_tools.kani_harness_cli_options import (
        DestinationFlags,
        PolicyFlags,
    )

    config = supervision_policy(
        PolicyFlags(watchdog=True, watchdog_mib=512),
        DestinationFlags(),
    )

    assert config.watchdog_threshold_bytes == 512 * 1024 * 1024


def test_a_disabled_watchdog_has_no_threshold() -> None:
    """A disabled watchdog clears the floor even when a MiB value is set."""
    from rust_prover_tools.kani_destinations import supervision_policy
    from rust_prover_tools.kani_harness_cli_options import (
        DestinationFlags,
        PolicyFlags,
    )

    config = supervision_policy(
        PolicyFlags(watchdog=False, watchdog_mib=512),
        DestinationFlags(),
    )

    assert config.watchdog_threshold_bytes is None


def test_the_default_watchdog_floor_reaches_the_policy() -> None:
    """The default floor is 1024 MiB and is mapped by the same rule.

    The literal is deliberate: this pins the documented default, so a change
    to it fails here rather than silently moving every run's watchdog floor.
    """
    from rust_prover_tools.kani_destinations import supervision_policy
    from rust_prover_tools.kani_harness_cli_options import (
        DestinationFlags,
        PolicyFlags,
    )

    config = supervision_policy(PolicyFlags(), DestinationFlags())

    assert config.watchdog_threshold_bytes == 1024 * 1024 * 1024


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
    )
    dependencies = KaniPreparationDependencies(
        metadata=lambda _root: metadata, prepare=prepare
    )

    with pytest.raises(InventoryDiscoveryError, match="cfg gate"):
        qualify_harnesses(options, dependencies)
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
    )
    dependencies = KaniPreparationDependencies(
        metadata=lambda _root: metadata, prepare=prepare
    )

    with pytest.raises(InventoryDiscoveryError, match="requires features"):
        qualify_harnesses(options, dependencies)
    assert qualified == []


def test_preparation_uses_the_injected_metadata_source(tmp_path: Path) -> None:
    """Preparation asks the injected provider, not Cargo, for metadata."""
    provider_calls: list[Path] = []

    def provider(root: Path) -> tuple[CargoPackage, ...]:
        """Record the request and supply no packages."""
        provider_calls.append(root)
        return ()

    options = KaniHarnessRunOptions(
        repo_root=tmp_path,
        route=LegacyRoute(installation=tmp_path, cwd=tmp_path),
        inventory=_inventory("harnesses::a"),
    )
    dependencies = KaniPreparationDependencies(
        metadata=provider, prepare=lambda route: _prepared()
    )

    preparation = qualify_harnesses(options, dependencies)

    assert provider_calls == []
    assert preparation.selected[0].harness == "harnesses::a"


def test_a_supplied_environment_reaches_route_qualification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The composition root passes the injected environment to the qualifier.

    The qualifier is replaced by a recorder, so the assertion is about the
    plumbing rather than about Kani: the supplied mapping must arrive intact,
    and the process environment must not be consulted for it.
    """
    import rust_prover_tools.kani_harness_run as run_module

    supplied = {"RPT_PROBE": "from-the-caller"}
    seen: list[dict[str, str]] = []

    def recording(route: object, *, environ: object, cwd: object) -> object:
        """Record the qualifier's inputs and return a prepared route."""
        del route, cwd
        seen.append(typ.cast("dict[str, str]", environ).copy())
        return _prepared()

    monkeypatch.setenv("RPT_PROBE", "from-the-process")
    monkeypatch.setattr(run_module, "prepare_route", recording)
    options = KaniHarnessRunOptions(
        repo_root=tmp_path,
        route=LegacyRoute(installation=tmp_path, cwd=tmp_path),
        inventory=_inventory("harnesses::a"),
    )
    dependencies = KaniPreparationDependencies(environ=supplied)

    qualify_harnesses(options, dependencies)

    assert seen == [supplied]


def test_the_process_environment_is_read_once_at_the_composition_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without an override, the qualifier receives the process environment."""
    import rust_prover_tools.kani_harness_run as run_module

    seen: list[dict[str, str]] = []

    def recording(route: object, *, environ: object, cwd: object) -> object:
        """Record the qualifier's inputs and return a prepared route."""
        del route, cwd
        seen.append(typ.cast("dict[str, str]", environ).copy())
        return _prepared()

    monkeypatch.setenv("RPT_PROBE", "from-the-process")
    monkeypatch.setattr(run_module, "prepare_route", recording)
    options = KaniHarnessRunOptions(
        repo_root=tmp_path,
        route=LegacyRoute(installation=tmp_path, cwd=tmp_path),
        inventory=_inventory("harnesses::a"),
    )

    qualify_harnesses(options)

    assert seen[0]["RPT_PROBE"] == "from-the-process"
