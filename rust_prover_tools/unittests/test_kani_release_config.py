"""Cargo discovery mutations that must stop competing verifier backends."""

from __future__ import annotations

import json
import typing as typ

import pytest

from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_release_config import (
    BackendSelection,
    VerificationConfig,
    check_verification_config,
    codegen_backend_variable,
)

if typ.TYPE_CHECKING:
    from pathlib import Path


CABOCHON_CONFIG = (
    "[unstable]\n"
    "codegen-backend = true\n"
    "\n"
    "[profile.dev]\n"
    'codegen-backend = "cranelift"\n'
)


def _write_config(root: Path, content: str) -> Path:
    """Create one Cargo configuration document below a discovered directory."""
    config = root / ".cargo"
    config.mkdir(parents=True, exist_ok=True)
    path = config / "config.toml"
    path.write_text(content, encoding="utf-8")
    return path


def _discover(root: Path) -> VerificationConfig:
    """Validate one owned discovery root and return the collected selections."""
    return check_verification_config(
        root,
        {"CARGO_HOME": str(root / "cargo-home")},
        search_roots=(root,),
    )


def test_cabochon_development_backend_is_accepted_and_recorded(
    tmp_path: Path,
) -> None:
    """The Cranelift enable flag plus a dev selection stays overridable."""
    path = _write_config(tmp_path, CABOCHON_CONFIG)
    assert _discover(tmp_path) == VerificationConfig(
        selections=(BackendSelection(path, "dev", "cranelift"),)
    )


def test_ancestor_configuration_selection_is_collected(tmp_path: Path) -> None:
    """A backend outside the proof cwd remains a relevant Cargo input."""
    path = _write_config(tmp_path, CABOCHON_CONFIG)
    cwd = tmp_path / "proof"
    cwd.mkdir()
    assert check_verification_config(
        cwd,
        {"CARGO_HOME": str(tmp_path / "cargo-home")},
        search_roots=(cwd, tmp_path),
    ) == VerificationConfig(selections=(BackendSelection(path, "dev", "cranelift"),))


def test_ancestor_manifest_profile_selection_is_collected(tmp_path: Path) -> None:
    """A workspace manifest profile override is overridable through the environment."""
    path = tmp_path / "Cargo.toml"
    path.write_text(
        "[workspace]\nmembers = []\n\n[profile.release-lto]\n"
        'inherits = "release"\ncodegen-backend = "cranelift"\n',
        encoding="utf-8",
    )
    cwd = tmp_path / "proof"
    cwd.mkdir()
    assert check_verification_config(
        cwd,
        {"CARGO_HOME": str(tmp_path / "cargo-home")},
        search_roots=(cwd, tmp_path),
    ) == VerificationConfig(
        selections=(BackendSelection(path, "release-lto", "cranelift"),)
    )


@pytest.mark.parametrize("enabled", [True, False])
def test_unstable_enable_flag_is_accepted_without_a_selection(
    tmp_path: Path, *, enabled: bool
) -> None:
    """The enable flag selects nothing on its own, so it needs no override."""
    literal = "true" if enabled else "false"
    _write_config(tmp_path, f"[unstable]\ncodegen-backend = {literal}\n")
    assert _discover(tmp_path) == VerificationConfig(selections=())


def test_profiles_are_collected_in_discovery_order(tmp_path: Path) -> None:
    """Nested configuration and ancestor manifests report each selection once."""
    cwd = tmp_path / "proof"
    cwd.mkdir()
    manifest = tmp_path / "Cargo.toml"
    manifest.write_text(
        "[workspace]\nmembers = []\n\n[profile.release]\n"
        'codegen-backend = "cranelift"\n',
        encoding="utf-8",
    )
    nested = _write_config(cwd, CABOCHON_CONFIG)
    assert check_verification_config(
        cwd,
        {"CARGO_HOME": str(tmp_path / "cargo-home")},
        search_roots=(cwd, tmp_path),
    ) == VerificationConfig(
        selections=(
            BackendSelection(nested, "dev", "cranelift"),
            BackendSelection(manifest, "release", "cranelift"),
        )
    )


@pytest.mark.parametrize(
    ("profile", "variable"),
    [
        ("dev", "CARGO_PROFILE_DEV_CODEGEN_BACKEND"),
        ("release-lto", "CARGO_PROFILE_RELEASE_LTO_CODEGEN_BACKEND"),
        ("release_lto", "CARGO_PROFILE_RELEASE_LTO_CODEGEN_BACKEND"),
        ("Release2", "CARGO_PROFILE_RELEASE2_CODEGEN_BACKEND"),
    ],
)
def test_codegen_backend_variable_names_the_profile(
    profile: str, variable: str
) -> None:
    """Cargo uppercases profile names and maps dashes to underscores."""
    assert codegen_backend_variable(profile) == variable


def test_hyphenated_and_underscored_profiles_share_one_variable(
    tmp_path: Path,
) -> None:
    """Distinct profile spellings may share one environment variable."""
    _write_config(
        tmp_path,
        '[profile.release-lto]\ncodegen-backend = "cranelift"\n'
        "\n"
        '[profile.release_lto]\ncodegen-backend = "cranelift"\n',
    )
    selections = _discover(tmp_path).selections
    assert {item.profile for item in selections} == {"release-lto", "release_lto"}
    assert {codegen_backend_variable(item.profile) for item in selections} == {
        "CARGO_PROFILE_RELEASE_LTO_CODEGEN_BACKEND"
    }


@pytest.mark.parametrize(
    ("content", "pattern"),
    [
        (
            '[profile.dev.package.proof]\ncodegen-backend = "cranelift"\n',
            "package-specific",
        ),
        (
            '[profile.dev.package."*"]\ncodegen-backend = "cranelift"\n',
            "package-specific",
        ),
        (
            '[profile.dev.build-override]\ncodegen-backend = "cranelift"\n',
            "backend",
        ),
        ("[profile.dev]\ncodegen-backend = 3\n", "backend"),
        ("[profile.dev]\ncodegen-backend = true\n", "backend"),
        (
            '[profile."bad profile"]\ncodegen-backend = "cranelift"\n',
            "backend",
        ),
        (
            '[profile.""]\ncodegen-backend = "cranelift"\n',
            "backend",
        ),
        ('[build]\ncodegen-backend = "cranelift"\n', "backend"),
        ('codegen-backend = "cranelift"\n', "backend"),
    ],
)
def test_refuses_backend_positions_that_cannot_be_overridden(
    tmp_path: Path, content: str, pattern: str
) -> None:
    """Every unoverridable position fails closed and names its source path."""
    path = _write_config(tmp_path, content)
    with pytest.raises(ProverToolError, match=pattern) as raised:
        _discover(tmp_path)
    message = str(raised.value)
    assert str(path) in message


def test_package_override_message_explains_the_refusal(tmp_path: Path) -> None:
    """A package override states why the environment cannot prove isolation."""
    _write_config(
        tmp_path,
        '[profile.dev.package.proof]\ncodegen-backend = "cranelift"\n',
    )
    with pytest.raises(ProverToolError) as raised:
        _discover(tmp_path)
    message = str(raised.value)
    assert "profile.dev.package.proof.codegen-backend" in message
    assert "CARGO_PROFILE_DEV_CODEGEN_BACKEND" in message


def test_manifest_backend_outside_profiles_is_ignored(tmp_path: Path) -> None:
    """Manifest tables other than [profile] cannot select a backend."""
    (tmp_path / "Cargo.toml").write_text(
        '[package]\nname = "proof"\nversion = "0.1.0"\ncodegen-backend = "cranelift"\n',
        encoding="utf-8",
    )
    assert _discover(tmp_path) == VerificationConfig(selections=())


def test_boolean_configuration_flag_is_rejected_in_manifests(
    tmp_path: Path,
) -> None:
    """Manifest profiles accept only string backend selections."""
    (tmp_path / "Cargo.toml").write_text(
        "[workspace]\nmembers = []\n\n[profile.dev]\ncodegen-backend = true\n",
        encoding="utf-8",
    )
    with pytest.raises(ProverToolError, match="backend"):
        _discover(tmp_path)


def test_unresolved_include_is_rejected(tmp_path: Path) -> None:
    """An unseen include cannot be presented as an isolated configuration."""
    config = tmp_path / ".cargo"
    config.mkdir()
    (config / "config.toml").write_text('include="other.toml"\n', encoding="utf-8")
    with pytest.raises(ProverToolError, match="include"):
        check_verification_config(
            tmp_path,
            {"CARGO_HOME": str(tmp_path / "cargo-home")},
            search_roots=(tmp_path,),
        )


def test_package_include_is_not_a_cargo_configuration_include(tmp_path: Path) -> None:
    """A publication file list does not select another compiler configuration."""
    (tmp_path / "Cargo.toml").write_text(
        '[package]\nname="proof"\nversion="0.1.0"\ninclude=["src/**"]\n',
        encoding="utf-8",
    )
    check_verification_config(
        tmp_path,
        {"CARGO_HOME": str(tmp_path / "cargo-home")},
        search_roots=(tmp_path,),
    )


@pytest.mark.parametrize("variable", ["LD_LIBRARY_PATH", "LD_PRELOAD"])
def test_forced_loader_configuration_cannot_replace_selected_runtime(
    tmp_path: Path, variable: str
) -> None:
    """Cargo-launched compilers cannot receive a different forced loader route."""
    config = tmp_path / ".cargo"
    config.mkdir()
    (config / "config.toml").write_text(
        f'[env]\n{variable}={{value="/other",force=true}}\n', encoding="utf-8"
    )
    with pytest.raises(ProverToolError, match="Cargo env overrides"):
        check_verification_config(
            tmp_path,
            {"LD_LIBRARY_PATH": "/selected", "CARGO_HOME": str(tmp_path / "home")},
            search_roots=(tmp_path,),
        )


@pytest.mark.parametrize(
    "content", ['[env]\nRUSTC_WRAPPER=""\n', '[build]\nrustc-wrapper="/other"\n']
)
def test_rejects_configuration_admission_override(tmp_path: Path, content: str) -> None:
    """Cargo cannot replace the admitted wrapper after child validation."""
    config = tmp_path / ".cargo"
    config.mkdir()
    (config / "config.toml").write_text(content, encoding="utf-8")
    with pytest.raises(ProverToolError, match=r"wrapper|routing"):
        check_verification_config(
            tmp_path,
            {"CARGO_HOME": str(tmp_path / "cargo-home")},
            search_roots=(tmp_path,),
        )


def test_relative_cargo_home_is_discovered_from_proof_cwd(tmp_path: Path) -> None:
    """A relative Cargo home cannot hide an overridable selection from validation."""
    home = tmp_path / "relative-cargo-home"
    home.mkdir()
    path = home / "config.toml"
    path.write_text('[profile.dev]\ncodegen-backend="cranelift"\n', encoding="utf-8")
    assert check_verification_config(
        tmp_path,
        {"CARGO_HOME": "relative-cargo-home"},
        search_roots=(tmp_path,),
    ) == VerificationConfig(selections=(BackendSelection(path, "dev", "cranelift"),))


def test_unresolved_include_takes_precedence_over_a_profile_selection(
    tmp_path: Path,
) -> None:
    """An unseen include is refused before any reachable selection is trusted."""
    _write_config(
        tmp_path,
        'include = "other.toml"\n\n[profile.dev]\ncodegen-backend = "cranelift"\n',
    )
    with pytest.raises(ProverToolError, match="include"):
        _discover(tmp_path)


@pytest.mark.parametrize(
    ("key", "variable"),
    [
        ("rustc-wrapper", "RUSTC_WRAPPER"),
        ("rustc-workspace-wrapper", "RUSTC_WORKSPACE_WRAPPER"),
        ("rustdoc", "RUSTDOC"),
    ],
)
def test_matching_config_route_preserves_inherited_selection(
    tmp_path: Path, key: str, variable: str
) -> None:
    """A matching Cargo default cannot compete with the selected admission route."""
    config = tmp_path / ".cargo"
    config.mkdir()
    (config / "config.toml").write_text(
        f'[build]\n{key}="/selected-admission"\n', encoding="utf-8"
    )
    check_verification_config(
        tmp_path,
        {variable: "/selected-admission", "CARGO_HOME": str(tmp_path / "cargo-home")},
        search_roots=(tmp_path,),
    )


@pytest.mark.parametrize("key", ["rustc-wrapper", "rustc-workspace-wrapper", "rustdoc"])
def test_unselected_config_route_is_rejected(tmp_path: Path, key: str) -> None:
    """Without inherited selection, a Cargo route has no admitted identity proof."""
    config = tmp_path / ".cargo"
    config.mkdir()
    (config / "config.toml").write_text(
        f'[build]\n{key}="/unproved-route"\n', encoding="utf-8"
    )
    with pytest.raises(ProverToolError, match="routing"):
        check_verification_config(
            tmp_path,
            {"CARGO_HOME": str(tmp_path / "cargo-home")},
            search_roots=(tmp_path,),
        )


@pytest.mark.parametrize(
    "variable", ["RUSTC_WRAPPER", "RUSTC_WORKSPACE_WRAPPER", "RUSTDOC"]
)
def test_forced_config_env_route_remains_rejected(
    tmp_path: Path, variable: str
) -> None:
    """A forced Cargo environment route remains unsupported even with a caller value."""
    config = tmp_path / ".cargo"
    config.mkdir()
    (config / "config.toml").write_text(
        f'[env]\n{variable}={{value="/other",force=true}}\n', encoding="utf-8"
    )
    with pytest.raises(ProverToolError, match="routing"):
        check_verification_config(
            tmp_path,
            {variable: "/selected", "CARGO_HOME": str(tmp_path / "cargo-home")},
            search_roots=(tmp_path,),
        )


def test_production_discovery_accepts_matching_admission_config(tmp_path: Path) -> None:
    """Accept matching inherited routes during automatic configuration discovery."""
    for ancestor in tmp_path.parents:
        if any(
            (ancestor / relative).exists()
            for relative in (
                "Cargo.toml",
                ".cargo/config",
                ".cargo/config.toml",
            )
        ):
            pytest.skip("automatic discovery has a foreign ancestor Cargo input")
    routes = {
        "rustc-wrapper": "RUSTC_WRAPPER",
        "rustc-workspace-wrapper": "RUSTC_WORKSPACE_WRAPPER",
        "rustdoc": "RUSTDOC",
    }
    environment = dict.fromkeys(routes.values(), "/selected-admission")
    environment["CARGO_HOME"] = str(tmp_path / "cargo-home")
    environment["HOME"] = str(tmp_path)
    config = tmp_path / ".cargo"
    config.mkdir()
    content = "[build]\n" + "".join(
        f"{key}={json.dumps(environment[variable])}\n"
        for key, variable in routes.items()
    )
    (config / "config.toml").write_text(content, encoding="utf-8")
    check_verification_config(tmp_path, environment)


@pytest.mark.parametrize(
    ("key", "variable"),
    [
        ("rustc-wrapper", "RUSTC_WRAPPER"),
        ("rustc-workspace-wrapper", "RUSTC_WORKSPACE_WRAPPER"),
        ("rustdoc", "RUSTDOC"),
    ],
)
def test_differing_config_fallback_is_shadowed_by_selected_environment(
    tmp_path: Path, key: str, variable: str
) -> None:
    """Bundled Cargo uses the valid inherited process value before a build fallback."""
    config = tmp_path / ".cargo"
    config.mkdir()
    (config / "config.toml").write_text(
        f'[build]\n{key}="/other-route"\n', encoding="utf-8"
    )
    check_verification_config(
        tmp_path,
        {variable: "/selected", "CARGO_HOME": str(tmp_path / "cargo-home")},
        search_roots=(tmp_path,),
    )


@pytest.mark.parametrize(
    ("key", "variable"),
    [
        ("rustc-wrapper", "RUSTC_WRAPPER"),
        ("rustc-workspace-wrapper", "RUSTC_WORKSPACE_WRAPPER"),
        ("rustdoc", "RUSTDOC"),
    ],
)
@pytest.mark.parametrize("selection", ["", "\udcff", "invalid\x00selection"])
def test_invalid_process_selection_cannot_prove_config_shadowing(
    tmp_path: Path, key: str, variable: str, selection: str
) -> None:
    """Presence alone cannot prove Cargo selects an admitted process route."""
    config = tmp_path / ".cargo"
    config.mkdir()
    (config / "config.toml").write_text(
        f'[build]\n{key}="/other-route"\n', encoding="utf-8"
    )
    with pytest.raises(ProverToolError, match="invalid process tool selection"):
        check_verification_config(
            tmp_path,
            {variable: selection, "CARGO_HOME": str(tmp_path / "cargo-home")},
            search_roots=(tmp_path,),
        )


@pytest.mark.parametrize(
    "variable", ["RUSTC_WRAPPER", "RUSTC_WORKSPACE_WRAPPER", "RUSTDOC"]
)
@pytest.mark.parametrize("value", ["", "\udcff", "invalid\x00selection"])
def test_invalid_process_tool_is_rejected_without_config(
    tmp_path: Path, variable: str, value: str
) -> None:
    """No competing config is needed to make malformed inherited tools unsafe."""
    with pytest.raises(ProverToolError, match="invalid process tool selection"):
        check_verification_config(
            tmp_path,
            {variable: value, "CARGO_HOME": str(tmp_path / "cargo-home")},
            search_roots=(tmp_path,),
        )


@pytest.mark.parametrize(
    "variable", ["RUSTC_WRAPPER", "RUSTC_WORKSPACE_WRAPPER", "RUSTDOC"]
)
@pytest.mark.parametrize("selected", [False, True])
def test_process_selection_controls_cargo_build_environment_fallback(
    tmp_path: Path, variable: str, *, selected: bool
) -> None:
    """Cargo's alternate build env route needs a proved process shadow."""
    environment = {
        "CARGO_BUILD_" + variable: "/other-route",
        "CARGO_HOME": str(tmp_path / "cargo-home"),
    }
    if selected:
        environment[variable] = "/selected"
        check_verification_config(tmp_path, environment, search_roots=(tmp_path,))
    else:
        with pytest.raises(ProverToolError, match="unproved Cargo compiler routing"):
            check_verification_config(
                tmp_path,
                environment,
                search_roots=(tmp_path,),
            )


@pytest.mark.parametrize(
    "variable", ["RUSTC_WRAPPER", "RUSTC_WORKSPACE_WRAPPER", "RUSTDOC"]
)
def test_forced_build_env_alias_is_rejected_despite_process_selection(
    tmp_path: Path, variable: str
) -> None:
    """Forced routing env aliases cannot hide behind an admitted process tool."""
    config = tmp_path / ".cargo"
    config.mkdir()
    (config / "config.toml").write_text(
        f'[env]\nCARGO_BUILD_{variable}={{value="/other",force=true}}\n',
        encoding="utf-8",
    )
    with pytest.raises(ProverToolError, match="Cargo env overrides"):
        check_verification_config(
            tmp_path,
            {variable: "/selected", "CARGO_HOME": str(tmp_path / "cargo-home")},
            search_roots=(tmp_path,),
        )
