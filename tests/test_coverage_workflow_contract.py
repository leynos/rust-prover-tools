"""Guard the main-owned CodeScene coverage boundary."""

import typing as typ
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
CI_PATH = ROOT / ".github" / "workflows" / "ci.yml"
MAIN_PATH = ROOT / ".github" / "workflows" / "coverage-main.yml"
REVISION = "152d9c4784d0ae5877938a984fe6d1f04d718fd8"


def _load(path: Path) -> dict[str, object]:
    """Load and normalize a workflow mapping."""
    workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(workflow, dict)
    if True in workflow:
        workflow["on"] = workflow.pop(True)
    return typ.cast("dict[str, object]", workflow)


def _mapping(value: object) -> dict[str, object]:
    """Return a validated workflow mapping."""
    assert isinstance(value, dict)
    return typ.cast("dict[str, object]", value)


def _job(workflow: dict[str, object]) -> dict[str, object]:
    """Return the workflow's coverage-owning job."""
    jobs = _mapping(workflow.get("jobs"))
    name = "coverage-upload" if "coverage-upload" in jobs else "lint-test"
    return _mapping(jobs.get(name))


def _step(job: dict[str, object], name: str) -> dict[str, object]:
    """Return a named workflow step."""
    steps = job.get("steps")
    assert isinstance(steps, list)
    for candidate in steps:
        if isinstance(candidate, dict):
            step = typ.cast("dict[str, object]", candidate)
            if step.get("name") == name:
                return step
    message = f"missing workflow step {name!r}"
    raise AssertionError(message)


def _inputs(step: dict[str, object]) -> dict[str, object]:
    """Return a step's validated input mapping."""
    return _mapping(step.get("with"))


def test_pull_request_coverage_is_local_and_ratcheted() -> None:
    """Keep CodeScene credentials and uploads out of pull-request CI."""
    workflow = _load(CI_PATH)
    coverage = _step(_job(workflow), "Test and Measure Coverage")
    assert coverage.get("if") == "github.event_name == 'pull_request'"
    assert coverage.get("uses") == (
        "leynos/shared-actions/.github/actions/generate-coverage@" + REVISION
    )
    inputs = _inputs(coverage)
    assert inputs.get("python-source") == "./rust_prover_tools"
    pytest_workers = inputs.get("pytest-workers")
    assert isinstance(pytest_workers, str)
    assert not pytest_workers
    assert inputs.get("with-ratchet") == "true"
    text = CI_PATH.read_text(encoding="utf-8")
    assert "CS_ACCESS_TOKEN" not in text
    assert "upload-codescene-coverage" not in text
    assert "cs-coverage" not in text


def test_main_publishes_the_ratcheted_measurement() -> None:
    """Keep CodeScene publication on main with explicit upload mode."""
    workflow = _load(MAIN_PATH)
    assert workflow.get("on") == {"push": {"branches": ["main"]}}
    job = _job(workflow)
    generate = _step(job, "Generate coverage")
    assert generate.get("uses") == (
        "leynos/shared-actions/.github/actions/generate-coverage@" + REVISION
    )
    inputs = _inputs(generate)
    assert inputs.get("python-source") == "./rust_prover_tools"
    pytest_workers = inputs.get("pytest-workers")
    assert isinstance(pytest_workers, str)
    assert not pytest_workers
    assert inputs.get("with-ratchet") == "true"
    upload = _step(job, "Upload coverage data to CodeScene")
    assert upload.get("uses") == (
        "leynos/shared-actions/.github/actions/upload-codescene-coverage@" + REVISION
    )
    assert _inputs(upload).get("mode") == "upload"
