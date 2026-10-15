"""Fail-closed filesystem and injected home-directory discovery contracts."""

from __future__ import annotations

import errno
import typing as typ
from pathlib import Path

import pytest

from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_release_config import (
    check_verification_config,
    checked_resolve,
    checked_stat,
)

if typ.TYPE_CHECKING:
    from os import stat_result


@pytest.mark.parametrize("error", [errno.EACCES, errno.EIO, errno.ELOOP])
def test_cargo_discovery_never_treats_probe_errors_as_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error: int,
) -> None:
    """A deterministic inaccessible config probe cannot silently pass as absent."""
    target = tmp_path / ".cargo/config.toml"
    original = Path.stat

    def inspect(path: Path, *, follow_symlinks: bool = True) -> stat_result:
        if path == target:
            raise OSError(error, "injected filesystem failure")
        return original(path, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(Path, "stat", inspect)
    with pytest.raises(ProverToolError, match="filesystem input"):
        check_verification_config(
            tmp_path,
            {"CARGO_HOME": str(tmp_path / "home")},
            search_roots=(tmp_path,),
        )


@pytest.mark.parametrize("error", [errno.ENOENT, errno.ENOTDIR])
def test_only_missing_components_are_absent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error: int,
) -> None:
    """Both forms of nonexistent path terminate a filesystem lookup safely."""

    def inspect(_path: Path, *, follow_symlinks: bool = True) -> stat_result:
        raise OSError(error, "injected missing component")

    monkeypatch.setattr(Path, "stat", inspect)
    assert checked_stat(tmp_path / "missing") is None


def test_resolution_failure_is_a_typed_validation_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A symlink loop cannot escape the expected CLI error boundary."""

    def resolve(_path: Path, *, strict: bool = False) -> Path:
        raise OSError(errno.ELOOP, "injected symlink loop")

    monkeypatch.setattr(Path, "resolve", resolve)
    with pytest.raises(ProverToolError, match="resolve filesystem input"):
        checked_resolve(tmp_path)


@pytest.mark.parametrize(
    "failure", [PermissionError("denied"), UnicodeError("bad encoding")]
)
def test_unreadable_cargo_file_is_a_typed_validation_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
) -> None:
    """A present config that cannot be read never counts as measured or safe."""
    config = tmp_path / ".cargo"
    config.mkdir()
    (config / "config.toml").touch()

    def read(
        _path: Path, encoding: str | None = None, errors: str | None = None
    ) -> str:
        raise failure

    monkeypatch.setattr(Path, "read_text", read)
    with pytest.raises(ProverToolError, match="could not validate Cargo"):
        check_verification_config(
            tmp_path,
            {"CARGO_HOME": str(tmp_path / "home")},
            search_roots=(tmp_path,),
        )


@pytest.mark.parametrize("selection", ["cargo_home", "home", "absent"])
def test_injected_home_selection_never_reads_host_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    selection: str,
) -> None:
    """Even a present injected HOME must not evaluate an ambient fallback."""

    def host_home() -> Path:
        pytest.fail("the host home is not part of the injected environment")

    monkeypatch.setattr(Path, "home", host_home)
    environment = {}
    if selection == "cargo_home":
        environment["CARGO_HOME"] = str(tmp_path / "cargo-home")
    elif selection == "home":
        environment["HOME"] = str(tmp_path)
    if selection == "absent":
        with pytest.raises(ProverToolError, match="injected HOME or CARGO_HOME"):
            check_verification_config(tmp_path, environment, search_roots=(tmp_path,))
    else:
        check_verification_config(tmp_path, environment, search_roots=(tmp_path,))


def test_package_metadata_reserved_words_are_not_cargo_controls(tmp_path: Path) -> None:
    """Only actual manifest profile tables can choose a manifest backend."""
    (tmp_path / "Cargo.toml").write_text(
        '[package.metadata.example]\ninclude="data"\ncodegen-backend="example"\n'
        '[package.metadata.example.env]\nRUSTC_WRAPPER="example"\n'
        '[package.metadata.example.build]\nrustc-wrapper="example"\n',
        encoding="utf-8",
    )
    environment = {"CARGO_HOME": str(tmp_path / "home")}
    check_verification_config(tmp_path, environment, search_roots=(tmp_path,))
    with (tmp_path / "Cargo.toml").open("a", encoding="utf-8") as manifest:
        manifest.write('[profile.dev]\ncodegen-backend="cranelift"\n')
    with pytest.raises(ProverToolError, match="backend"):
        check_verification_config(tmp_path, environment, search_roots=(tmp_path,))
