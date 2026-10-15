"""Cargo discovery mutations that must stop competing verifier backends."""

from __future__ import annotations

import json
import os
import typing as typ

import pytest

from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_release_config import check_verification_config

if typ.TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.parametrize("filename", ["Cargo.toml", ".cargo/config.toml"])
def test_detects_ancestor_backend(tmp_path: Path, filename: str) -> None:
    """A backend outside the proof cwd remains a relevant Cargo input."""
    path = tmp_path / filename
    path.parent.mkdir(exist_ok=True)
    path.write_text('[profile.dev]\ncodegen-backend="cranelift"\n', encoding="utf-8")
    cwd = tmp_path / "proof"
    cwd.mkdir()
    with pytest.raises(ProverToolError, match="backend"):
        check_verification_config(
            cwd,
            {"CARGO_HOME": str(tmp_path / "cargo-home")},
            search_roots=(cwd, tmp_path),
        )


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
    """A relative Cargo home cannot hide an independent backend from validation."""
    home = tmp_path / "relative-cargo-home"
    home.mkdir()
    (home / "config.toml").write_text(
        '[profile.dev]\ncodegen-backend="cranelift"\n', encoding="utf-8"
    )
    with pytest.raises(ProverToolError, match="backend"):
        check_verification_config(
            tmp_path,
            {"CARGO_HOME": "relative-cargo-home"},
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
    routes = {
        "rustc-wrapper": "RUSTC_WRAPPER",
        "rustc-workspace-wrapper": "RUSTC_WORKSPACE_WRAPPER",
        "rustdoc": "RUSTDOC",
    }
    environment = {
        variable: os.environ.get(variable, "/selected-admission")
        for variable in routes.values()
    }
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
