"""Child-scoped execution contracts for the legacy release boundary."""

from __future__ import annotations

import os
import typing as typ

import pytest

from rust_prover_tools.commands import CommandResult, run_command
from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_release import KaniExecOptions, prepare_kani_command

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

    from rust_prover_tools.commands import CommandSpec
    from rust_prover_tools.kani_release_config import VerificationConfig


@pytest.fixture(autouse=True)
def owned_config_discovery(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Inert runtime cases exercise real validation using only owned config inputs."""
    from rust_prover_tools import kani_release
    from rust_prover_tools.kani_release_config import check_verification_config

    def validate(cwd: Path, environment: cabc.Mapping[str, str]) -> VerificationConfig:
        return check_verification_config(cwd, environment, search_roots=(tmp_path,))

    monkeypatch.setattr(kani_release, "check_verification_config", validate)


@pytest.fixture
def installed_release(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Create an inert release tree with the exact qualified metadata."""
    monkeypatch.setattr(
        "rust_prover_tools.kani_release.platform.machine", lambda: "x86_64"
    )
    root = tmp_path / "kani-0.67.0"
    for directory in ("bin", "toolchain/bin", "toolchain/lib"):
        (root / directory).mkdir(parents=True)
    (root / "rustc-version").write_text(
        "rustc 1.93.0-nightly (53732d5e0 2025-11-20)\n", encoding="utf-8"
    )
    (root / "rust-toolchain-version").write_text(
        "nightly-2025-11-21-x86_64-unknown-linux-gnu\n", encoding="utf-8"
    )
    for name in (
        "bin/kani-driver",
        "bin/kani-compiler",
        "toolchain/bin/rustc",
        "toolchain/bin/cargo",
    ):
        path = root / name
        script = (
            '#!/bin/sh\nprintf "rustc 1.93.0-nightly (53732d5e0 2025-11-20)\\n"\n'
            if name in {"bin/kani-compiler", "toolchain/bin/rustc"}
            else "#!/bin/sh\nexit 0\n"
        )
        path.write_text(script, encoding="utf-8")
        path.chmod(0o755)
    (root / "toolchain/lib/libLLVM.so.21.1-rust-1.93.0-nightly").touch()
    return root


def test_runtime_path_wrappers_flags_and_literal_arguments(
    installed_release: Path, tmp_path: Path
) -> None:
    """The launch specification preserves admission and proof invocation."""
    environment = {
        "LD_LIBRARY_PATH": "/custom:/rustup/toolchains/nightly/lib:/other",
        "RUSTC_WRAPPER": "/admission rustc",
        "RUSTC_WORKSPACE_WRAPPER": "/workspace-wrapper",
        "RUSTDOC": "/admission rustdoc",
        "RUSTFLAGS": "-D warnings -Zthreads=8 -Clink-arg=-fuse-ld=mold",
        "CARGO_HOME": str(tmp_path / "cargo-home"),
    }
    before = dict(environment)
    options = KaniExecOptions(installed_release, tmp_path, ("list", "space ; $literal"))
    spec = prepare_kani_command(options, environment)
    assert spec.cwd == tmp_path
    assert spec.argv[-2:] == ("list", "space ; $literal")
    assert spec.env is not None
    assert (
        spec.env["LD_LIBRARY_PATH"]
        == f"{installed_release}/toolchain/lib:/custom:/other"
    )
    for key in ("RUSTC_WRAPPER", "RUSTC_WORKSPACE_WRAPPER", "RUSTDOC"):
        assert spec.env[key] == environment[key]
    assert spec.env["PATH"].split(os.pathsep)[0] == str(
        installed_release / "toolchain/bin"
    )
    assert spec.env["RUSTFLAGS"] == "-D warnings"
    assert spec.env["CARGO_ENCODED_RUSTFLAGS"] == "-D\x1fwarnings"
    assert environment == before


def test_profile_backend_selection_is_pinned_for_the_child(
    installed_release: Path,
    tmp_path: Path,
    cargo_profile_project: cabc.Callable[..., dict[str, str]],
) -> None:
    """A repository development backend cannot reach the selected release."""
    environment = cargo_profile_project("dev")

    spec = prepare_kani_command(
        KaniExecOptions(installed_release, tmp_path, ("list",)), environment
    )

    assert spec.env is not None, "a prepared command must carry a child environment"
    assert spec.env["CARGO_PROFILE_DEV_CODEGEN_BACKEND"] == "llvm", (
        "the development profile must be pinned to LLVM for the child"
    )


def test_profile_override_stays_child_scoped(
    installed_release: Path,
    tmp_path: Path,
    cargo_profile_project: cabc.Callable[..., dict[str, str]],
) -> None:
    """The caller's environment is never mutated to carry the override."""
    environment = cargo_profile_project("dev")
    before = dict(environment)

    prepare_kani_command(
        KaniExecOptions(installed_release, tmp_path, ("list",)), environment
    )

    assert environment == before, "the caller's environment must not be mutated"


def test_every_development_profile_is_pinned_independently(
    installed_release: Path,
    tmp_path: Path,
    cargo_profile_project: cabc.Callable[..., dict[str, str]],
) -> None:
    """Hyphenated, custom, and test profiles each receive their own shadow."""
    environment = cargo_profile_project("dev", "release-dev", "test")

    spec = prepare_kani_command(
        KaniExecOptions(installed_release, tmp_path, ("list",)), environment
    )

    assert spec.env is not None, "a prepared command must carry a child environment"
    assert spec.env["CARGO_PROFILE_DEV_CODEGEN_BACKEND"] == "llvm", (
        "the selected release must not inherit the dev backend"
    )
    assert spec.env["CARGO_PROFILE_RELEASE_DEV_CODEGEN_BACKEND"] == "llvm", (
        "a hyphenated profile must be pinned to LLVM for the child"
    )
    assert spec.env["CARGO_PROFILE_TEST_CODEGEN_BACKEND"] == "llvm", (
        "the test profile must be pinned to LLVM for the child"
    )


def _backend_event(caplog: pytest.LogCaptureFixture) -> dict[str, object]:
    """Return the fields of the single backend-override decision event."""
    events = [
        record.__dict__
        for record in caplog.records
        if record.__dict__.get("stage") == "backend_overrides"
    ]
    assert len(events) == 1, "exactly one backend override decision must be logged"
    return events[0]


def test_unselected_repository_logs_no_override_event(
    installed_release: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A repository without profile backends records no override decision."""
    caplog.set_level("INFO", logger="rust_prover_tools.kani_release")

    spec = prepare_kani_command(
        KaniExecOptions(installed_release, tmp_path, ("list",)),
        {"CARGO_HOME": str(tmp_path / "cargo-home")},
    )

    assert spec.env is not None, "a prepared command must carry a child environment"
    assert not any(
        key.startswith("CARGO_PROFILE_") and key.endswith("_CODEGEN_BACKEND")
        for key in spec.env
    ), "an unselected repository must not receive a profile override"
    assert not [
        record
        for record in caplog.records
        if record.__dict__.get("stage") == "backend_overrides"
    ], "an unselected repository must not report an override decision"


def test_backend_override_event_reports_one_selection(
    installed_release: Path,
    tmp_path: Path,
    cargo_profile_project: cabc.Callable[..., dict[str, str]],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A single accepted profile reports one selection and one variable."""
    caplog.set_level("INFO", logger="rust_prover_tools.kani_release")

    prepare_kani_command(
        KaniExecOptions(installed_release, tmp_path, ("list",)),
        cargo_profile_project("dev"),
    )

    event = _backend_event(caplog)
    assert event["stage"] == "backend_overrides", "the event names the stage"
    assert event["operation"] == "backend_overrides", "the event names its operation"
    assert event["release"] == "0.67.0", "the event names the qualified release"
    assert event["status_class"] == "success", "the override completed"
    assert event["error_category"] == "none", "no failure category applies"
    duration = event["duration_seconds"]
    assert isinstance(duration, float), "the event reports a real duration"
    assert duration >= 0, "the observed duration cannot be negative"
    assert event["selection_count"] == 1, "one profile selection was discovered"
    assert event["variable_count"] == 1, "one environment variable was written"
    assert event["collision_count"] == 0, "a single spelling cannot collide"


def test_backend_override_event_reports_collisions(
    installed_release: Path,
    tmp_path: Path,
    cargo_profile_project: cabc.Callable[..., dict[str, str]],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Two spellings of one variable report one written variable and a collision."""
    caplog.set_level("INFO", logger="rust_prover_tools.kani_release")

    prepare_kani_command(
        KaniExecOptions(installed_release, tmp_path, ("list",)),
        cargo_profile_project("release-lto", "release_lto"),
    )

    event = _backend_event(caplog)
    assert event["selection_count"] == 2, "both profile spellings were discovered"
    assert event["variable_count"] == 1, "both spellings share one variable"
    assert event["collision_count"] == 1, "one pair of spellings collided"


def test_backend_override_event_distinguishes_duplicates_from_collisions(
    installed_release: Path,
    tmp_path: Path,
    cargo_profile_project: cabc.Callable[..., dict[str, str]],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """One profile declared twice is a duplicate, not a spelling collision."""
    caplog.set_level("INFO", logger="rust_prover_tools.kani_release")
    (tmp_path / "Cargo.toml").write_text(
        '[profile.dev]\ncodegen-backend = "cranelift"\n', encoding="utf-8"
    )

    prepare_kani_command(
        KaniExecOptions(installed_release, tmp_path, ("list",)),
        cargo_profile_project("dev"),
    )

    event = _backend_event(caplog)
    assert event["selection_count"] == 2, "both declarations were discovered"
    assert event["variable_count"] == 1, "both declarations name one variable"
    assert event["collision_count"] == 0, "a repeated spelling does not collide"


def test_proof_failure_preserves_backend_and_stage_events(
    installed_release: Path,
    tmp_path: Path,
    cargo_profile_project: cabc.Callable[..., dict[str, str]],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The new decision event does not replace existing stage reporting."""
    from rust_prover_tools.kani_release import exec_kani

    compiler = installed_release / "toolchain/bin/rustc"
    compiler.write_text(
        '#!/bin/sh\nprintf "rustc 1.93.0-nightly (53732d5e0 2025-11-20)\\n"\n',
        encoding="utf-8",
    )
    (installed_release / "bin/kani-compiler").write_text(
        compiler.read_text(encoding="utf-8"), encoding="utf-8"
    )
    driver = installed_release / "bin/kani-driver"
    driver.write_text(
        (
            '#!/bin/sh\nif [ "$1" = "kani" ] && [ "$2" = "--version" ]; '
            'then printf "kani 0.67.0\\n"; exit 0; fi\nexit 23\n'
        ),
        encoding="utf-8",
    )
    environment = cargo_profile_project("dev")
    caplog.set_level("INFO", logger="rust_prover_tools.kani_release")

    assert (
        exec_kani(KaniExecOptions(installed_release, tmp_path, ("list",)), environment)
        == 23
    ), "the verifier failure must survive stage observation"
    stages = [
        record.__dict__
        for record in caplog.records
        if record.getMessage() == "Kani execution stage completed"
    ]
    assert [stage["stage"] for stage in stages] == [
        "backend_overrides",
        "validation",
        "rustc_identity",
        "compiler_identity",
        "verifier_identity",
        "proof",
    ], "the decision and every execution stage must both report, in order"
    proof = next(stage for stage in stages if stage["stage"] == "proof")
    assert proof["status_class"] == "nonzero", "the proof failure must be classified"
    assert proof["error_category"] == "command", "the failure came from the command"
    assert _backend_event(caplog)["selection_count"] == 1, (
        "the backend decision must be recorded alongside stage events"
    )


@pytest.mark.parametrize(
    "argument",
    [
        "setup",
        "--config=profile.dev.codegen-backend=cranelift",
        "--profile",
        "--manifest-path",
    ],
)
def test_rejects_arguments_that_evade_the_checked_route(
    installed_release: Path, tmp_path: Path, argument: str
) -> None:
    """Execution cannot become installation or select an unchecked config root."""
    with pytest.raises(ProverToolError):
        prepare_kani_command(
            KaniExecOptions(installed_release, tmp_path, (argument,)), {}
        )


def test_rejects_mismatched_runtime_metadata(
    installed_release: Path, tmp_path: Path
) -> None:
    """Directory names cannot establish the selected release identity."""
    (installed_release / "rustc-version").write_text(
        "rustc another\n", encoding="utf-8"
    )
    with pytest.raises(ProverToolError, match="identity"):
        prepare_kani_command(
            KaniExecOptions(installed_release, tmp_path, ("list",)), {}
        )


def test_real_cuprum_child_retains_scope_and_nonzero_status(
    installed_release: Path, tmp_path: Path
) -> None:
    """An inert child exposes literal arguments, cwd and child-only environment."""
    driver = installed_release / "bin/kani-driver"
    driver.write_text(
        (
            '#!/bin/sh\nprintf "%s\\n" "$PWD" "$RUSTC_WRAPPER" "$RUSTDOC" '
            '"$LD_LIBRARY_PATH" "$@"\nprintf "child-stderr\\n" >&2\nexit 23\n'
        ),
        encoding="utf-8",
    )
    environment = dict(os.environ)
    environment.update({
        "RUSTC_WRAPPER": "/test-admission",
        "RUSTDOC": "/test-rustdoc",
        "CARGO_HOME": str(tmp_path / "cargo-home"),
    })
    before = dict(os.environ)
    result = run_command(
        prepare_kani_command(
            KaniExecOptions(installed_release, tmp_path, ("list", "space ; $literal")),
            environment,
        )
    )
    assert result.exit_code == 23
    assert result.stderr == "child-stderr\n"
    assert "space ; $literal\n" in result.stdout
    assert "/test-admission\n/test-rustdoc\n" in result.stdout
    assert result.stdout.splitlines()[0] == str(tmp_path)
    assert os.environ == before


def test_identity_queries_use_admission_and_proof_failure_survives(
    installed_release: Path, tmp_path: Path
) -> None:
    """Actual Cuprum execution retains admission even for compiler identity."""
    from rust_prover_tools.kani_release import exec_kani

    compiler = installed_release / "toolchain/bin/rustc"
    compiler.write_text(
        '#!/bin/sh\nprintf "rustc 1.93.0-nightly (53732d5e0 2025-11-20)\\n"\n',
        encoding="utf-8",
    )
    trace = tmp_path / "admitted"
    (installed_release / "bin/kani-compiler").write_text(
        compiler.read_text(encoding="utf-8"), encoding="utf-8"
    )
    wrapper = tmp_path / "wrapper"
    wrapper.write_text(
        f'#!/bin/sh\nprintf "%s\\n" "$1" >> "{trace}"\nexec "$@"\n',
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    driver = installed_release / "bin/kani-driver"
    driver.write_text(
        (
            '#!/bin/sh\nif [ "$1" = "kani" ] && [ "$2" = "--version" ]; '
            'then printf "kani 0.67.0\\n"; exit 0; fi\nprintf '
            '"proof-output\\n"\nprintf "proof-error\\n" >&2\nexit 23\n'
        ),
        encoding="utf-8",
    )
    environment = dict(os.environ)
    environment.update({
        "RUSTC_WRAPPER": str(wrapper),
        "CARGO_HOME": str(tmp_path / "cargo-home"),
    })
    assert (
        exec_kani(KaniExecOptions(installed_release, tmp_path, ("list",)), environment)
        == 23
    )
    assert trace.read_text(encoding="utf-8").splitlines() == [
        str(compiler),
        str(installed_release / "bin/kani-compiler"),
    ]


def test_public_cli_preserves_verifier_failure(
    installed_release: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The public command cannot turn a verifier failure into CLI success."""
    from rust_prover_tools.cli import app

    compiler = installed_release / "toolchain/bin/rustc"
    compiler.write_text(
        '#!/bin/sh\nprintf "rustc 1.93.0-nightly (53732d5e0 2025-11-20)\\n"\n',
        encoding="utf-8",
    )
    (installed_release / "bin/kani-compiler").write_text(
        compiler.read_text(encoding="utf-8"), encoding="utf-8"
    )
    wrapper = tmp_path / "wrapper"
    wrapper.write_text('#!/bin/sh\nexec "$@"\n', encoding="utf-8")
    wrapper.chmod(0o755)
    driver = installed_release / "bin/kani-driver"
    driver.write_text(
        (
            '#!/bin/sh\nif [ "$1" = "kani" ] && [ "$2" = "--version" ]; '
            'then printf "kani 0.67.0\\n"; exit 0; fi\nexit 23\n'
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("RUSTC_WRAPPER", str(wrapper))
    monkeypatch.setenv("CARGO_HOME", str(tmp_path / "cargo-home"))
    result = app([
        "kani",
        "exec",
        "--installation",
        str(installed_release),
        "--cwd",
        str(tmp_path),
        "--extra-arg=list",
    ])
    assert result == 23


def test_driver_symlink_cannot_select_another_installation(
    installed_release: Path, tmp_path: Path
) -> None:
    """Keep the driver in the installation selected by current_exe."""
    driver = installed_release / "bin/kani-driver"
    other = tmp_path / "other-driver"
    driver.rename(other)
    driver.symlink_to(other)
    with pytest.raises(ProverToolError, match="selected installation"):
        prepare_kani_command(
            KaniExecOptions(installed_release, tmp_path, ("list",)), {}
        )


def test_verifier_binary_drift_is_rejected_before_proof(
    installed_release: Path, tmp_path: Path
) -> None:
    """Metadata alone cannot qualify a different verifier binary."""
    from rust_prover_tools.kani_release import exec_kani

    driver = installed_release / "bin/kani-driver"
    marker = tmp_path / "proof-started"
    driver.write_text(
        '#!/bin/sh\nif [ "$2" = "--version" ]; '
        'then printf "kani 0.68.0\\n"; exit 0; fi\n'
        f'touch "{marker}"\n',
        encoding="utf-8",
    )
    wrapper = tmp_path / "wrapper"
    wrapper.write_text('#!/bin/sh\nexec "$@"\n', encoding="utf-8")
    wrapper.chmod(0o755)
    with pytest.raises(ProverToolError, match="verifier identity"):
        exec_kani(
            KaniExecOptions(installed_release, tmp_path, ("list",)),
            {"RUSTC_WRAPPER": str(wrapper), "CARGO_HOME": str(tmp_path / "cargo-home")},
        )
    assert not marker.exists()


@pytest.mark.parametrize(
    ("installation", "cwd"),
    [
        ("./sdk", "../proof"),
        ("~/sdk", "$HOME/proof"),
        ("/selected sdk", "/proof directory"),
    ],
)
def test_public_cli_converts_literal_path_arguments(
    installation: str, cwd: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CLI path text retains relative paths, spaces and literal expansion markers."""
    from pathlib import Path

    from rust_prover_tools import kani_exec_cli
    from rust_prover_tools.cli import app

    seen: list[KaniExecOptions] = []

    def record(options: KaniExecOptions, _environment: cabc.Mapping[str, str]) -> int:
        seen.append(options)
        return 23

    monkeypatch.setattr(kani_exec_cli, "exec_kani", record)
    result = app([
        "kani",
        "exec",
        "--installation",
        installation,
        "--cwd",
        cwd,
        "--extra-arg=list",
    ])
    assert result == 23
    assert seen == [KaniExecOptions(Path(installation), Path(cwd), ("list",))]


@pytest.mark.parametrize(
    ("outer", "workspace"),
    [
        (None, None),
        ("/admission", None),
        (None, "/workspace"),
        ("/admission", "/workspace"),
    ],
    ids=["hosted_without_wrappers", "outer", "workspace_only", "both"],
)
def test_public_cli_preserves_optional_compiler_wrappers(
    installed_release: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    outer: str | None,
    workspace: str | None,
) -> None:
    """Hosted absence and each configured wrapper preserve the identity chain."""
    from rust_prover_tools import kani_release
    from rust_prover_tools.cli import app

    selected = {
        "RUSTC_WRAPPER": outer,
        "RUSTC_WORKSPACE_WRAPPER": workspace,
        "RUSTDOC": "/kept-rustdoc" if outer or workspace else None,
    }
    for variable, value in selected.items():
        if value is None:
            monkeypatch.delenv(variable, raising=False)
        else:
            monkeypatch.setenv(variable, value)
    for suffix in ("RUSTC_WRAPPER", "RUSTC_WORKSPACE_WRAPPER", "RUSTDOC"):
        monkeypatch.delenv("CARGO_BUILD_" + suffix, raising=False)
    monkeypatch.setenv("CARGO_HOME", str(tmp_path / "cargo-home"))
    seen: list[CommandSpec] = []

    def capture(spec: CommandSpec) -> CommandResult:
        seen.append(spec)
        if spec.argv[-1] != "--version":
            return CommandResult("proof-output", "proof-error", 23)
        identity = (
            "kani 0.67.0"
            if spec.argv[0].endswith("/kani-driver")
            else "rustc 1.93.0-nightly (53732d5e0 2025-11-20)"
        )
        return CommandResult(identity, "", 0)

    monkeypatch.setattr(kani_release, "run_command", capture)
    assert (
        app([
            "kani",
            "exec",
            "--installation",
            str(installed_release),
            "--cwd",
            str(tmp_path),
            "--extra-arg=list",
        ])
        == 23
    )
    chain = tuple(value for value in (outer, workspace) if value is not None)
    assert len(seen) == 4
    for spec, relative in zip(
        seen[:2], ("toolchain/bin/rustc", "bin/kani-compiler"), strict=True
    ):
        assert spec.argv == (*chain, str(installed_release / relative), "--version")
        assert spec.env is not None
        for variable, value in selected.items():
            assert spec.env.get(variable) == value
