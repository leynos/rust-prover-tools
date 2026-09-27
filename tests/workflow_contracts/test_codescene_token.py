"""Refusal cases for the availability check, the upload guard and the token.

Each case changes one thing in the compliant fixture tree and asserts on
the one rule that must refuse it, so deleting that rule's clause fails
the case.
"""

from __future__ import annotations

import pytest

from .codescene_token import (
    check_step_violations,
    token_scope_violations,
    upload_guard_violations,
    upload_token_violations,
)
from .fixtures import PUBLISHER, mutate, replaced
from .loading import Document, WorkflowReadingError, load_workflow

AVAILABLE = "steps.codescene-token.outputs.available == 'true'"
GUARD = f"if: {AVAILABLE} && github.ref == 'refs/heads/main'"
CHECK = (
    "        run: echo \"available=${{ secrets.CS_ACCESS_TOKEN != '' }}\""
    ' >> "$GITHUB_OUTPUT"\n'
)
CHECK_ID = "        id: codescene-token\n"
CHECK_STEP = "      - name: Check for the CodeScene token\n" + CHECK_ID + CHECK
UPLOAD_NAME = "      - name: Upload coverage data to CodeScene\n"
READ = "${{ secrets.CS_ACCESS_TOKEN }}"


def _publisher(texts: dict[str, str]) -> Document:
    """Return the parsed publisher of a tree."""
    return load_workflow(texts["coverage-main.yml"])


@pytest.mark.parametrize(
    "guard",
    [
        # Every required term stays whole; only the `||` refusal catches it.
        f"{GUARD} && github.actor != 'x' || github.event_name == 'workflow_dispatch'",
        f"if: {AVAILABLE}",
        "if: github.ref == 'refs/heads/main'",
        f"if: ${{{{ !({AVAILABLE} && github.ref == 'refs/heads/main') }}}}",
        f"if: ({AVAILABLE} && github.ref == 'refs/heads/main'",
        f"if: {AVAILABLE} && github.ref != 'refs/heads/main'",
        "if: env.CS_ACCESS_TOKEN != '' && github.ref == 'refs/heads/main'",
    ],
)
def test_the_upload_guard_needs_both_terms_and_no_disjunction(guard: str) -> None:
    """The ref guard and the check's output must hold as whole terms."""
    found = upload_guard_violations(
        _publisher(mutate("coverage-main.yml", GUARD, guard))
    )
    assert found, found


@pytest.mark.parametrize(
    "guard",
    [
        f"{GUARD} && github.actor != 'x'",
        f"{GUARD} && (github.actor != 'x' || github.run_attempt == '1')",
        f"if: ${{{{ github.ref == 'refs/heads/main' && {AVAILABLE} }}}}",
        f"{GUARD} && 'a||b' != ''",
    ],
)
def test_a_narrower_upload_guard_is_accepted(guard: str) -> None:
    """Extra terms, a wrapper and a quoted `||` do not trip the guard rule."""
    found = upload_guard_violations(
        _publisher(mutate("coverage-main.yml", GUARD, guard))
    )
    assert not found, found


def test_a_deleted_check_is_refused() -> None:
    """Without the check the guard can never be true, so the upload never runs."""
    with pytest.raises(WorkflowReadingError, match="check the token exactly once"):
        check_step_violations(_publisher(mutate("coverage-main.yml", CHECK_STEP, "")))


@pytest.mark.parametrize(
    "command",
    [
        '        run: echo "available=true" >> "$GITHUB_OUTPUT"\n',
        "        run: false && " + CHECK.removeprefix("        run: "),
    ],
)
def test_a_changed_check_command_is_refused(command: str) -> None:
    """Only the exact command counts as the check; anything else is no check."""
    with pytest.raises(WorkflowReadingError, match="check the token exactly once"):
        check_step_violations(_publisher(mutate("coverage-main.yml", CHECK, command)))


@pytest.mark.parametrize(
    "addition",
    [
        "        if: github.ref == 'refs/heads/main'\n",
        "        env:\n          X: y\n",
    ],
)
def test_the_check_runs_unconditionally_and_binds_nothing(addition: str) -> None:
    """An `if:` could skip the check, and an `env` is where a token would go."""
    texts = mutate("coverage-main.yml", CHECK_ID, CHECK_ID + addition)
    found = check_step_violations(_publisher(texts))
    assert found, found


def test_the_check_needs_an_id() -> None:
    """The guard reads the check by its id; a check without one reads nothing."""
    found = check_step_violations(_publisher(mutate("coverage-main.yml", CHECK_ID, "")))
    assert "the check step has no id" in found, found


def test_the_check_must_precede_the_upload() -> None:
    """A step output is only readable by the steps after it."""
    last = "          access-token: ${{ secrets.CS_ACCESS_TOKEN }}\n"
    text = replaced(replaced(PUBLISHER, CHECK_STEP, ""), last, last + CHECK_STEP)
    found = check_step_violations(load_workflow(text))
    assert found, found


@pytest.mark.parametrize(
    "value",
    ["${{ env.CS_ACCESS_TOKEN }}", "${{ steps.codescene-token.outputs.token }}", ""],
)
def test_the_uploader_reads_the_secret_directly(value: str) -> None:
    """The token reaches the uploader only as its `access-token` input."""
    old = "          access-token: ${{ secrets.CS_ACCESS_TOKEN }}\n"
    new = f"          access-token: {value}\n" if value else ""
    found = upload_token_violations(_publisher(mutate("coverage-main.yml", old, new)))
    assert found, found


@pytest.mark.parametrize(
    ("old", "new"),
    [
        (
            UPLOAD_NAME,
            UPLOAD_NAME + "        env:\n          CS_ACCESS_TOKEN: " + READ + "\n",
        ),
        (
            CHECK_ID,
            CHECK_ID + "        env:\n          T: ${{ secrets.CS_ACCESS_TOKEN }}\n",
        ),
        (
            "    runs-on: ubuntu-latest\n",
            "    runs-on: ubuntu-latest\n    env:\n      T: " + READ + "\n",
        ),
        (
            "          mode: upload\n",
            "          mode: upload\n          other: ${{ secrets.CS_ACCESS_TOKEN }}\n",
        ),
    ],
)
def test_the_token_is_refused_anywhere_else(old: str, new: str) -> None:
    """The upload step's env, the check's env, the job and another input are refused."""
    found = token_scope_violations(_publisher(mutate("coverage-main.yml", old, new)))
    assert found, found
