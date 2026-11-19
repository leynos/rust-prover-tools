"""End-to-end CLI contracts for the isolated snapshot commands."""

from __future__ import annotations

import typing as typ

import pytest

from rust_prover_tools import kani_snapshot_cli
from rust_prover_tools.cli import app
from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_snapshot.install_model import MANIFEST_ASSET_NAME
from tests.kani_snapshot_fixtures import make_pin, pin_bytes

if typ.TYPE_CHECKING:
    from pathlib import Path

    from rust_prover_tools.downloads import PinnedAsset
    from rust_prover_tools.kani_snapshot.download import (
        DownloadSettings,
        ReleaseUrlPolicy,
    )
    from rust_prover_tools.kani_snapshot.install_model import InstallOptions


def _write_pin(tmp_path: Path) -> Path:
    """Write a valid reviewed pin and return its path."""
    path = tmp_path / "snapshot.toml"
    path.write_bytes(pin_bytes(make_pin()))
    return path


def test_check_reports_a_missing_installation(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Checking an empty cache fails without inventing a generation."""
    pin = _write_pin(tmp_path)
    status = app(
        [
            "kani",
            "snapshot",
            "check",
            "--pin",
            str(pin),
            "--cache-dir",
            str(tmp_path / "cache"),
        ],
        exit_on_error=False,
    )
    captured = capsys.readouterr()
    assert status == 1, "a missing installation must exit with status 1"
    assert "not installed" in captured.err, "stderr did not report the empty cache"


def test_install_without_assets_fetches_the_release_the_pin_names(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The default transport downloads the reviewed release, not a local flag.

    Only the very first download is intercepted: it proves the CLI selected the
    release transport and asked it for the manifest, and keeps the test away
    from the network. A refusal that asked for `--asset-dir` would mean the
    documented default path is missing.
    """
    pin = _write_pin(tmp_path)
    requested: list[str] = []

    def refuse(
        asset: PinnedAsset,
        destination: Path,
        policy: ReleaseUrlPolicy,
        settings: DownloadSettings,
    ) -> Path:
        """Record the requested asset and report an unreachable release."""
        del destination, settings
        requested.append(asset.name)
        msg = f"release unavailable: {policy.asset_url()}"
        raise ProverToolError(msg)

    monkeypatch.setattr(
        "rust_prover_tools.kani_snapshot.install_sender.download_asset",
        refuse,
    )
    status = app(
        [
            "kani",
            "snapshot",
            "install",
            "--pin",
            str(pin),
            "--cache-dir",
            str(tmp_path / "cache"),
        ],
        exit_on_error=False,
    )
    captured = capsys.readouterr()
    assert status == 1, "an unreachable release must exit with status 1"
    assert requested == [MANIFEST_ASSET_NAME], "the manifest was not requested"
    assert "--asset-dir" not in captured.err, "the default transport was refused"
    assert make_pin().tag in captured.err, "stderr did not name the release"


#: Forwarded spellings that would replace the pinned backend, with the phrase
#: the refusal must contain. Each is refused before a generation is consulted.
_BACKEND_REPLACEMENTS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("setup",), "setup command"),
    (("kani", "setup"), "setup command"),
    (("--use-local-bundle", "bundle.tar.gz"), "setup flag"),
    (("--use-local-bundle=bundle.tar.gz",), "setup flag"),
    (("--use-local-toolchain", "/opt/toolchain"), "setup flag"),
    (("--use-local-toolchain=/opt/toolchain",), "setup flag"),
    (("-Zlean",), "Lean backend"),
    (("-Z", "lean"), "Lean backend"),
    (("-Z=lean",), "Lean backend"),
    (("--unstable", "lean"), "Lean backend"),
    (("--unstable=lean",), "Lean backend"),
)


@pytest.mark.parametrize(("forwarded", "reason"), _BACKEND_REPLACEMENTS)
def test_run_rejects_a_backend_replacing_argument(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    forwarded: tuple[str, ...],
    reason: str,
) -> None:
    """A forwarded argument that replaces the pinned backend is refused."""
    pin = _write_pin(tmp_path)
    status = app(
        [
            "kani",
            "run",
            "--snapshot-pin",
            str(pin),
            "--cache-dir",
            str(tmp_path / "cache"),
            "--",
            *forwarded,
        ],
        exit_on_error=False,
    )
    captured = capsys.readouterr()
    assert status == 1, "the refusal must exit with status 1"
    assert reason in captured.err, f"stderr did not mention {reason!r}"


def test_relative_paths_resolve_below_the_repository_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A relative pin and cache anchor at the root, not the caller's cwd."""
    _write_pin(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    seen: list[Path] = []

    def recording_check(options: InstallOptions) -> typ.NoReturn:
        """Record the resolved cache, then refuse as an empty cache would."""
        assert options.cache_home is not None, "the CLI resolved no cache directory"
        seen.append(options.cache_home)
        msg = "snapshot is not installed"
        raise ProverToolError(msg)

    monkeypatch.setattr(kani_snapshot_cli, "check_active_snapshot", recording_check)

    status = app(
        [
            "kani",
            "snapshot",
            "check",
            "--repo-root",
            str(tmp_path),
            "--pin",
            "snapshot.toml",
            "--cache-dir",
            "cache",
        ],
        exit_on_error=False,
    )
    captured = capsys.readouterr()
    assert status == 1, "a missing installation must exit with status 1"
    assert "not installed" in captured.err, "the relative pin did not resolve"
    assert seen == [tmp_path / "cache"], f"the cache resolved to {seen}"
    assert not (elsewhere / "cache").exists(), "the cache resolved against the cwd"


def test_absolute_paths_ignore_the_repository_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An absolute pin stays where the caller put it, even outside the root."""
    root = tmp_path / "repo"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    absolute_pin = _write_pin(outside)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    status = app(
        [
            "kani",
            "snapshot",
            "check",
            "--repo-root",
            str(root),
            "--pin",
            str(absolute_pin),
            "--cache-dir",
            "cache",
        ],
        exit_on_error=False,
    )
    captured = capsys.readouterr()
    assert status == 1, "a missing installation must exit with status 1"
    assert "not installed" in captured.err, "the absolute pin was re-anchored"


def test_snapshot_flags_do_not_inherit_generic_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Generic `INPUT_PIN` selection cannot redirect a snapshot command."""
    pin = _write_pin(tmp_path)
    monkeypatch.setenv("INPUT_PIN", str(tmp_path / "absent.toml"))
    status = app(
        [
            "kani",
            "snapshot",
            "check",
            "--pin",
            str(pin),
            "--cache-dir",
            str(tmp_path / "cache"),
        ],
        exit_on_error=False,
    )
    captured = capsys.readouterr()
    assert status == 1, "a missing installation must exit with status 1"
    assert "not installed" in captured.err, "stderr did not report the empty cache"
    assert "absent.toml" not in captured.err, "a generic INPUT_PIN redirected the run"
