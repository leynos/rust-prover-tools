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
_USERS_GUIDE = REPO_ROOT / "docs" / "users-guide.md"
#: The identifier the guide gives its smoke-dispatch example.
_SMOKE_EXAMPLE = "kani-snapshot-smoke"
#: The placeholders the example shows, with representative values substituted.
_SUBSTITUTIONS = {"<full upstream commit>": _SHA, "<12 hex>": "0123456789ab"}


def _make(
    *arguments: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Run this repository's Makefile and capture what it prints or runs."""
    return subprocess.run(
        [MAKE or "make", "--no-print-directory", "-f", "Makefile", *arguments],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        # The caller asserts the status and output it needs, so a failing
        # recipe is a test result rather than an exception to handle here.
        check=False,
    )


def _gh_stub(tmp_path: Path) -> tuple[dict[str, str], Path]:
    """Return an environment whose ``gh`` records the arguments it receives."""
    binary = tmp_path / "bin" / "gh"
    binary.parent.mkdir()
    binary.write_text(_GH_STUB, encoding="utf-8")
    binary.chmod(0o755)
    log = tmp_path / "gh-args"
    environment = {**os.environ, "GH_ARGS_FILE": str(log)}
    environment["PATH"] = f"{binary.parent}{os.pathsep}{environment['PATH']}"
    return environment, log


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
    environment, log = _gh_stub(tmp_path)
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
    assert log.read_text(encoding="utf-8") == (
        "workflow\nrun\nkani-monthly.yml\n--ref\nmain\n-f\n"
        f"upstream_commit={_SHA}\n-f\npublication_id={_TAG}\n-f\npublish=false\n"
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
    example = load_examples(
        _USERS_GUIDE.read_text(encoding="utf-8"), source=str(_USERS_GUIDE)
    )[_SMOKE_EXAMPLE]
    command = example.body
    for placeholder, value in _SUBSTITUTIONS.items():
        assert placeholder in command, (
            f"the example no longer shows the placeholder {placeholder!r}"
        )
        command = command.replace(placeholder, value)
    environment, log = _gh_stub(tmp_path)
    shell = shutil.which("sh")
    assert shell is not None, "a POSIX shell is required to run the example"

    completed = subprocess.run(
        [shell, "-eu", "-c", command],
        cwd=REPO_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert log.read_text(encoding="utf-8") == (
        "workflow\nrun\nkani-monthly.yml\n--ref\nmain\n-f\n"
        f"upstream_commit={_SHA}\n-f\npublication_id={_TAG}\n-f\npublish=false\n"
    ), "the documented command did not dispatch a non-publishing run"
