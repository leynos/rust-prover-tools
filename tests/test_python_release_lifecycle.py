"""Behavioural tests for the wheel release lifecycle.

The contract module pins the workflow's text; this module runs the workflow's
own steps against a stand-in `gh`, so each recovery shape is shown to make
exactly the writes it claims. D5 asks for regression coverage of interrupted
drafts, published no-ops and collisions, and a published no-op is only a no-op
if nothing was created, uploaded or edited.

The stand-in records every invocation, so a test can assert both the outcome
and the writes. Its state lives in a directory whose layout mirrors what the
release step reads back: the draft flag, the served assets and the call log.
"""

from __future__ import annotations

import dataclasses as dc
import json
import os
import shutil
import subprocess
import typing as typ

import pytest

from tests.workflow_contracts.strict_workflow import (
    REPO_ROOT,
    job,
    named_scripts,
    read_workflow,
)

if typ.TYPE_CHECKING:
    import pathlib

#: The workflow whose steps are executed here.
RELEASE_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "release.yml"

#: The tag the stand-in release is served under.
TAG = "v0.2.0"

#: The one wheel the workflow's build would produce.
WHEEL = "rust_prover_tools-0.2.0-py3-none-any.whl"

#: Bytes the local build and the served release must agree on.
WHEEL_BYTES = b"PK\x03\x04 pretend wheel"

#: Steps the release job runs, in the order the runner would run them.
STEPS = (
    "Require a non-empty wheel inventory",
    "Create the draft or resume it",
    "Upload the missing wheels",
    "Verify the complete asset set",
    "Publish the release",
)

#: The stand-in `gh`. It serves and mutates one recorded release so a test can
#: observe both what the workflow read and what it wrote.
_FAKE_GH = '''\
#!/usr/bin/env python3
"""Serve and mutate one recorded release, logging every invocation."""

import fnmatch
import json
import pathlib
import shutil
import sys

STATE = pathlib.Path(__import__("os").environ["FAKE_GH_STATE"])
STATE_FILE = STATE / "state.json"
ASSETS = STATE / "assets"
CALLS = STATE / "calls.log"


def load():
    """Return the recorded release, or None when it does not exist."""
    if not STATE_FILE.is_file():
        return None
    return json.loads(STATE_FILE.read_text(encoding="utf-8"))


def save(state):
    """Persist the recorded release."""
    STATE_FILE.write_text(json.dumps(state), encoding="utf-8")


def record(argv):
    """Append one invocation to the call log."""
    with CALLS.open("a", encoding="utf-8") as handle:
        handle.write(" ".join(argv) + "\\n")


def served():
    """Return the names of the assets the release serves."""
    return sorted(path.name for path in ASSETS.iterdir())


def main(argv):
    """Dispatch one `gh release` invocation."""
    if argv[:1] != ["release"]:
        print("unsupported command: " + " ".join(argv), file=sys.stderr)
        return 2
    action = argv[1]
    if action == "view":
        state = load()
        if state is None:
            print("release not found", file=sys.stderr)
            return 1
        if argv[argv.index("--json") + 1] == "isDraft":
            print("true" if state["draft"] else "false")
        else:
            for name in served():
                print(name)
        return 0
    if action == "create":
        save({"tag": argv[2], "draft": True})
        ASSETS.mkdir(exist_ok=True)
        record(argv)
        return 0
    if action == "upload":
        shutil.copyfile(argv[3], ASSETS / pathlib.Path(argv[3]).name)
        record(argv)
        return 0
    if action == "download":
        target = pathlib.Path(argv[argv.index("--dir") + 1])
        pattern = argv[argv.index("--pattern") + 1]
        target.mkdir(parents=True, exist_ok=True)
        for name in served():
            if fnmatch.fnmatch(name, pattern):
                shutil.copyfile(ASSETS / name, target / name)
        record(argv)
        return 0
    if action == "edit":
        state = load()
        state["draft"] = "--draft=false" not in argv
        save(state)
        record(argv)
        return 0
    print("unsupported action: " + action, file=sys.stderr)
    return 2


raise SystemExit(main(sys.argv[1:]))
'''


@dc.dataclass
class _Lifecycle:
    """One workspace whose steps share the runner's environment rules."""

    workspace: pathlib.Path
    env: dict[str, str]

    def run(
        self, step: str, scripts: dict[str, str]
    ) -> subprocess.CompletedProcess[str]:
        """Run one workflow step, then apply the variables it exported."""
        bash = shutil.which("bash")
        assert bash is not None, "the workflow steps are Bash scripts"
        env_file = self.workspace / "github-env"
        env_file.write_text("", encoding="utf-8")
        result = subprocess.run(
            [bash, "-euo", "pipefail", "-c", scripts[step]],
            cwd=self.workspace,
            env={**self.env, "GITHUB_ENV": str(env_file)},
            capture_output=True,
            text=True,
            # A failing step is the expected outcome in the collision tests,
            # so its status is asserted rather than raised.
            check=False,
        )
        for line in env_file.read_text(encoding="utf-8").splitlines():
            key, separator, value = line.partition("=")
            if separator:
                self.env[key] = value
        return result

    def run_all(
        self, scripts: dict[str, str]
    ) -> list[subprocess.CompletedProcess[str]]:
        """Run the release steps as the runner would, stopping at a failure."""
        results: list[subprocess.CompletedProcess[str]] = []
        for step in STEPS:
            results.append(self.run(step, scripts))
            if results[-1].returncode != 0:
                break
        return results

    def calls(self) -> list[str]:
        """Return every invocation the stand-in `gh` served."""
        log = self.workspace.parent / "state" / "calls.log"
        return log.read_text(encoding="utf-8").splitlines()

    def writes(self) -> list[str]:
        """Return the invocations that changed the release."""
        return [
            call
            for call in self.calls()
            if call.startswith(("release create", "release upload", "release edit"))
        ]


def _lifecycle(
    tmp_path: pathlib.Path,
    *,
    served: dict[str, bytes] | None,
    draft: bool = False,
) -> _Lifecycle:
    """Create a workspace and a release serving `served`, when not None."""
    workspace = tmp_path / "workspace"
    (workspace / "dist").mkdir(parents=True)
    (workspace / "dist" / WHEEL).write_bytes(WHEEL_BYTES)

    state = tmp_path / "state"
    (state / "assets").mkdir(parents=True)
    (state / "calls.log").write_text("", encoding="utf-8")
    if served is not None:
        (state / "state.json").write_text(
            json.dumps({"tag": TAG, "draft": draft}), encoding="utf-8"
        )
        for name, data in served.items():
            (state / "assets" / name).write_bytes(data)

    binaries = tmp_path / "bin"
    binaries.mkdir()
    gh = binaries / "gh"
    gh.write_text(_FAKE_GH, encoding="utf-8")
    gh.chmod(0o755)

    return _Lifecycle(
        workspace=workspace,
        env={
            "PATH": f"{binaries}{os.pathsep}{os.environ.get('PATH', '')}",
            "GITHUB_REF_NAME": TAG,
            "FAKE_GH_STATE": str(state),
            "HOME": str(tmp_path),
        },
    )


@pytest.fixture(scope="module")
def scripts() -> dict[str, str]:
    """Return the release job's scripts, keyed by step name."""
    return named_scripts(job(read_workflow(RELEASE_WORKFLOW), "release"))


def test_a_fresh_tag_creates_uploads_verifies_and_publishes(
    tmp_path: pathlib.Path,
    scripts: dict[str, str],
) -> None:
    """The ordinary path still writes once, in the safe order."""
    lifecycle = _lifecycle(tmp_path, served=None)

    results = lifecycle.run_all(scripts)

    assert [result.returncode for result in results] == [0] * len(STEPS)
    assert lifecycle.writes() == [
        f"release create {TAG} --draft --generate-notes --verify-tag --title {TAG}",
        f"release upload {TAG} dist/{WHEEL}",
        f"release edit {TAG} --draft=false",
    ]


def test_an_interrupted_draft_uploads_only_what_is_missing(
    tmp_path: pathlib.Path,
    scripts: dict[str, str],
) -> None:
    """A resumed draft adds the missing wheel and then publishes."""
    lifecycle = _lifecycle(tmp_path, served={}, draft=True)

    results = lifecycle.run_all(scripts)

    assert [result.returncode for result in results] == [0] * len(STEPS)
    assert f"release upload {TAG} dist/{WHEEL}" in lifecycle.writes()
    assert lifecycle.writes()[-1] == f"release edit {TAG} --draft=false"
    assert "release create" not in " ".join(lifecycle.writes())


def test_a_matching_published_release_is_a_no_op(
    tmp_path: pathlib.Path,
    scripts: dict[str, str],
) -> None:
    """The release is read and verified, and nothing is written."""
    lifecycle = _lifecycle(tmp_path, served={WHEEL: WHEEL_BYTES}, draft=False)

    results = lifecycle.run_all(scripts)

    assert [result.returncode for result in results] == [0] * len(STEPS)
    assert lifecycle.writes() == []
    assert any(call.startswith("release download") for call in lifecycle.calls())
    assert "serves exactly these bytes" in results[-2].stdout


@pytest.mark.parametrize(
    ("served", "message"),
    [
        pytest.param({}, "does not serve the expected wheel set", id="missing"),
        pytest.param(
            {WHEEL: WHEEL_BYTES, "notes.txt": b"notes"},
            "outside the expected inventory",
            id="extra-asset",
        ),
        pytest.param({WHEEL: b"different bytes"}, None, id="changed-bytes"),
    ],
)
def test_a_published_release_that_disagrees_is_a_collision(
    tmp_path: pathlib.Path,
    scripts: dict[str, str],
    served: dict[str, bytes],
    message: str | None,
) -> None:
    """Verification fails and no write is attempted, so bytes are never replaced."""
    lifecycle = _lifecycle(tmp_path, served=served, draft=False)

    results = lifecycle.run_all(scripts)

    assert results[-1].returncode != 0
    assert lifecycle.writes() == []
    if message is not None:
        assert message in results[-1].stderr


def test_a_collision_never_reaches_the_publication_step(
    tmp_path: pathlib.Path,
    scripts: dict[str, str],
) -> None:
    """A failed verification stops the job before the release could be edited."""
    lifecycle = _lifecycle(tmp_path, served={}, draft=False)

    results = lifecycle.run_all(scripts)

    assert len(results) == len(STEPS) - 1, "publication must never run"
    assert [result.returncode for result in results[:-1]] == [0] * (len(results) - 1)
    assert results[-1].returncode != 0
    assert "release edit" not in " ".join(lifecycle.calls())


def test_the_steps_are_the_ones_this_module_runs(
    scripts: dict[str, str],
) -> None:
    """A renamed step cannot silently drop a scenario from this suite."""
    assert set(scripts) >= set(STEPS), sorted(set(STEPS) - set(scripts))
