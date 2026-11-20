"""Contract tests for the Makefile targets that exercise the workflows.

The mutation caller's own contract lives in `tests.test_workflow_contract`;
this module covers the recipes that run the workflow checks. ``workflow-check``
must stay a strict subset of ``all`` so the pull request gate and the local
full run cannot drift apart, and the snapshot smoke dispatch must refuse
malformed inputs before it reaches ``gh`` and pass its inputs as data rather
than as shell words.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.documentation_examples import load_examples

REPO_ROOT = Path(__file__).resolve().parents[1]

MAKE = shutil.which("make")
NEEDS_MAKE = pytest.mark.skipif(MAKE is None, reason="GNU make is required")
_SHA = "a" * 40
_TAG = "kani-snapshot-2026-12-0123456789ab-r1"
_GH_STUB = '#!/bin/sh\nprintf \'%s\\n\' "$@" > "$GH_ARGS_FILE"\n'
#: A `gh` the sandboxed home provides. It must never run: the Makefile
#: prepends `$(HOME)/.local/bin` to PATH, so a real `gh` there would shadow the
#: stub and dispatch a hosted workflow.
_GH_DECOY = '#!/bin/sh\n: > "$GH_DECOY_FILE"\nexit 1\n'
_USERS_GUIDE = REPO_ROOT / "docs" / "users-guide.md"
#: The identifier the guide gives its smoke-dispatch example.
_SMOKE_EXAMPLE = "kani-snapshot-smoke"
#: The placeholders the example shows, with representative values substituted.
_SUBSTITUTIONS = {"<full upstream commit>": _SHA, "<12 hex>": "0123456789ab"}


def _make(
    *arguments: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Run this repository's Makefile and capture what it prints or runs.

    When the caller supplies an environment, its PATH is passed as a make
    command-line variable as well. The Makefile prepends `$(HOME)/.local/bin`
    to PATH, which would otherwise put a `gh` from the sandboxed home ahead of
    the stub the caller placed first; a command-line variable takes precedence
    over the Makefile's own assignment and is still exported to the recipe.
    """
    overrides = [f"PATH={env['PATH']}"] if env is not None and "PATH" in env else []
    return subprocess.run(
        [
            MAKE or "make",
            "--no-print-directory",
            "-f",
            "Makefile",
            *overrides,
            *arguments,
        ],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        # The caller asserts the status and output it needs, so a failing
        # recipe is a test result rather than an exception to handle here.
        check=False,
    )


def _gh_stub(
    tmp_path: Path, *, reader_home: bool = False
) -> tuple[dict[str, str], Path, Path]:
    """Return an environment whose ``gh`` stub alone can be reached.

    The stub records the arguments it receives. The sandboxed home always
    holds a `gh` under `.local/bin`, which is where the Makefile's PATH
    prepend looks first. By default it is a decoy that fails and records
    itself, so a make test that lost its PATH override is visible as a
    failure. A command run as a reader would run it — the documented example
    types `make` without overrides — so `reader_home` places the recording
    stub there instead, modelling the `gh` that reader has installed.
    """
    binary = tmp_path / "bin" / "gh"
    binary.parent.mkdir()
    binary.write_text(_GH_STUB, encoding="utf-8")
    binary.chmod(0o755)
    home = tmp_path / "home"
    home_gh = home / ".local" / "bin" / "gh"
    home_gh.parent.mkdir(parents=True)
    home_gh.write_text(_GH_STUB if reader_home else _GH_DECOY, encoding="utf-8")
    home_gh.chmod(0o755)
    log = tmp_path / "gh-args"
    decoy_log = tmp_path / "gh-decoy"
    environment = {
        **os.environ,
        "GH_ARGS_FILE": str(log),
        "GH_DECOY_FILE": str(decoy_log),
        "HOME": str(home),
    }
    environment["PATH"] = f"{binary.parent}{os.pathsep}{environment['PATH']}"
    return environment, log, decoy_log


@NEEDS_MAKE
def test_all_and_workflow_check_share_the_actionlint_target() -> None:
    """``all`` runs the full suite once; ``workflow-check`` keeps the subset."""
    all_run = _make("-n", "all")
    check_run = _make("-n", "workflow-check")
    assert all_run.returncode == check_run.returncode == 0, all_run.stderr
    assert "actionlint .github/workflows/*.yml" in all_run.stdout
    assert "actionlint .github/workflows/*.yml" in check_run.stdout
    assert "tests/test_kani_workflow_contract.py" not in all_run.stdout
    assert "tests/test_kani_workflow_contract.py" in check_run.stdout


@NEEDS_MAKE
def test_the_smoke_target_validates_its_inputs_and_dispatches_as_data(
    tmp_path: Path,
) -> None:
    """Malformed inputs stop before ``gh``; a valid one reaches it as data."""
    environment, log, decoy_log = _gh_stub(tmp_path)
    for commit, tag, message in [
        ("abc123", _TAG, "full 40-hex"),
        (_SHA, "kani-snapshot-2026-13-0123456789ab-r1", "kani-snapshot-YYYY-MM"),
    ]:
        refused = _make(
            "kani-snapshot-smoke",
            f"KANI_SNAPSHOT_UPSTREAM_COMMIT={commit}",
            f"KANI_SNAPSHOT_PUBLICATION_ID={tag}",
            env=environment,
        )
        assert refused.returncode != 0
        assert message in refused.stderr
    assert not log.exists(), "gh ran before an input was validated"
    dry_run = _make(
        "-n",
        "kani-snapshot-smoke",
        f"KANI_SNAPSHOT_UPSTREAM_COMMIT={_SHA}",
        f"KANI_SNAPSHOT_PUBLICATION_ID={_TAG}",
    )
    assert '-f upstream_commit="$KANI_SNAPSHOT_UPSTREAM_COMMIT"' in dry_run.stdout
    assert '-f publication_id="$KANI_SNAPSHOT_PUBLICATION_ID"' in dry_run.stdout
    done = _make(
        "kani-snapshot-smoke",
        f"KANI_SNAPSHOT_UPSTREAM_COMMIT={_SHA}",
        f"KANI_SNAPSHOT_PUBLICATION_ID={_TAG}",
        env=environment,
    )
    assert done.returncode == 0, done.stderr
    assert not decoy_log.exists(), (
        "the recipe reached the home directory's gh instead of the stub"
    )
    assert log.read_text(encoding="utf-8") == (
        "workflow\nrun\nkani-monthly.yml\n--ref\nmain\n-f\n"
        f"upstream_commit={_SHA}\n-f\npublication_id={_TAG}\n-f\npublish=false\n"
    )


def _documented_command() -> str:
    """Return the guide's smoke example with its placeholders filled in."""
    example = load_examples(
        _USERS_GUIDE.read_text(encoding="utf-8"), source=str(_USERS_GUIDE)
    )[_SMOKE_EXAMPLE]
    command = example.body
    for placeholder, value in _SUBSTITUTIONS.items():
        assert placeholder in command, (
            f"the example no longer shows the placeholder {placeholder!r}"
        )
        command = command.replace(placeholder, value)
    return command


def _run_example(
    command: str, environment: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    """Run the guide's command the way a reader would, with ``gh`` isolated.

    ``UV`` is pinned to the bare name in the child environment. When it is an
    absolute path the Makefile puts its directory first on ``PATH`` so the
    helper finds that uv, and a ``gh`` sitting beside it would then run ahead
    of the reader's own — dispatching the hosted workflow with the caller's
    credentials. Pinning the variable isolates that edge without adding an
    override to the documented command.
    """
    shell = shutil.which("sh")
    assert shell is not None, "a POSIX shell is required to run the example"
    return subprocess.run(
        [shell, "-eu", "-c", command],
        cwd=REPO_ROOT,
        env={**environment, "UV": "uv"},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


@NEEDS_MAKE
def test_the_documented_smoke_example_dispatches_without_publishing(
    tmp_path: Path,
) -> None:
    """The guide's own command runs, and the dispatch it makes cannot publish.

    The example is read from the shipped guide rather than restated here, so
    the documented invocation and the tested recipe are checked against each
    other: an edit to either one that breaks the reader's command fails this
    test, without dispatching a hosted run.
    """
    environment, log, _ = _gh_stub(tmp_path, reader_home=True)

    completed = _run_example(_documented_command(), environment)

    assert completed.returncode == 0, completed.stderr
    assert log.read_text(encoding="utf-8") == (
        "workflow\nrun\nkani-monthly.yml\n--ref\nmain\n-f\n"
        f"upstream_commit={_SHA}\n-f\npublication_id={_TAG}\n-f\npublish=false\n"
    ), "the documented command did not dispatch a non-publishing run"


@NEEDS_MAKE
def test_a_gh_beside_an_inherited_uv_cannot_shadow_the_reader_s_own(
    tmp_path: Path,
) -> None:
    """An inherited absolute ``UV`` must not put a ``gh`` of its own first.

    ``UV`` reaches the Makefile from the caller's environment, and its
    directory is prepended to ``PATH``. A checkout whose uv shares a directory
    with a ``gh`` would then dispatch the hosted workflow with the caller's
    credentials, so the example runner pins ``UV`` to the bare name: the decoy
    beside the uv must never run, and the reader's own stub must answer.
    """
    real_uv = shutil.which("uv")
    assert real_uv is not None, "the suite needs uv on the PATH"
    uv_directory = tmp_path / "uvbin"
    uv_directory.mkdir()
    (uv_directory / "uv").symlink_to(real_uv)
    decoy = uv_directory / "gh"
    decoy.write_text(_GH_DECOY, encoding="utf-8")
    decoy.chmod(0o755)
    environment, log, decoy_log = _gh_stub(tmp_path, reader_home=True)
    environment["UV"] = str(uv_directory / "uv")

    completed = _run_example(_documented_command(), environment)

    assert completed.returncode == 0, completed.stderr
    assert not decoy_log.exists(), (
        "a gh beside the inherited uv dispatched the hosted run"
    )
    assert log.read_text(encoding="utf-8").startswith("workflow\nrun\n"), (
        "the reader's own gh stub did not receive the dispatch"
    )
