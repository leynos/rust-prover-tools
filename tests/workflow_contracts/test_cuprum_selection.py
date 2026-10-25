"""Contract: every dependency path selects one exact cuprum version.

The project pins cuprum exactly rather than with a range. Cuprum 0.2 is a beta
whose prerelease line is still moving, and a range would let a later beta --
which may bring further breaking changes, as beta1 to beta2 did -- reach this
repository without a review of what changed. The pin is the review boundary.

Dependabot edits ``pyproject.toml`` and ``uv.lock`` and its pull requests
auto-merge, so a bump can land without a human reading it. Three sites must
therefore agree: the declared requirement, the version ``uv.lock`` resolves,
and the requirement ``uv.lock`` records. The version the test interpreter has
installed is a fourth: ``make build`` installs from the lock, so a disagreement
means the gates are testing something other than the committed selection.

The expected version is read from ``pyproject.toml``; this module holds no
version literal. A bump that misses a site fails, and the message names the
site that lagged.

The freshness check is separate and reads the Git index rather than the working
tree. ``make build`` runs ``uv sync``, which rewrites a stale ``uv.lock`` in
place, so a tree-based freshness assertion could never fail under ``make
test``: the gate would repair the condition before asserting it. Rebuilding
the pair from ``git show`` in a scratch directory is what makes the check able
to fail at all.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from .cuprum_selection import (
    SelectionError,
    declared_pin,
    installed_version,
    lock_pin,
    lock_specifier,
    names_cuprum,
    pin_from,
)

#: Both subprocesses are resolved to a full path rather than left to ``PATH``
#: lookup at exec time, so a test failure names an interpreter or Git, not an
#: empty string.
UV_BINARY = shutil.which("uv") or "uv"
GIT_BINARY = shutil.which("git") or "git"

#: Both freshness checks need the index and are skipped for the same reason
#: when it is absent.
_NO_GIT_CHECKOUT = (
    "no Git checkout to read indexed files from (for example in mutmut's sandbox)"
)

ROOT = Path(__file__).resolve().parents[2]
PYPROJECT = ROOT / "pyproject.toml"
LOCKFILE = ROOT / "uv.lock"

pytestmark = pytest.mark.timeout(60)


def test_the_name_matcher_agrees_with_pep_503_on_what_is_cuprum() -> None:
    """Only spellings that normalise to ``cuprum`` count as naming it.

    The readers treat "the site names no cuprum" and "the site names cuprum
    with the wrong specifier" as different failures, so the matcher deciding
    which is which has to agree with the index. ``Cuprum`` is the same
    distribution; ``cup_rum`` normalises to ``cup-rum``, which is not.
    """
    assert names_cuprum("cuprum==0.2.0b2"), "the bare name must match"
    assert names_cuprum("Cuprum==0.2.0b2"), "case must fold"
    assert names_cuprum("cuprum[extra]"), "an extra still names the project"
    assert names_cuprum("cuprum>=0.1"), "a range still names the project"
    assert not names_cuprum("cup_rum==0.2.0b2"), "a separator makes a new project"
    assert not names_cuprum("cutprum==0.2.0b2"), "a different name must not match"


@pytest.mark.parametrize(
    ("requirements", "reason"),
    [
        (["cuprum>=0.1"], "a range admits a version nobody reviewed"),
        (["cuprum==0.2.0b2", "cuprum==0.2.0b3"], "one site, two answers"),
        (["cuprum==0.2.0b2; python_version > '3.13'"], "a marker splits the site"),
        (["cuprum[rust]==0.2.0b2"], "an extra selects a different artefact"),
        (["cyclopts>=4"], "cuprum must be named at all"),
    ],
)
def test_pin_from_rejects_anything_but_one_exact_pin(
    requirements: list[str], reason: str
) -> None:
    """The reader refuses each shape that would defeat the contract.

    Each case is one way a dependency site can look pinned while leaving the
    resolved version open. The message is asserted to be a
    :class:`SelectionError` so a caller cannot mistake this for an import or
    parse failure.
    """
    with pytest.raises(SelectionError):
        pin_from(requirements, site=f"site rejected: {reason}")


def test_declared_pin_reads_the_project_requirement() -> None:
    """``pyproject.toml``'s own requirement is read, not a literal."""
    text = '[project]\nname = "demo"\ndependencies = ["cuprum==0.2.0b2", "cyclopts"]\n'

    assert declared_pin(text) == "0.2.0b2"


@pytest.mark.parametrize(
    "lock_text",
    [
        "[package]\nname = 'demo'\n",
        (
            "[[package]]\nname = 'cuprum'\nversion = '0.2.0b2'\n\n"
            "[[package]]\nname = 'cuprum'\nversion = '0.1.0'\n"
        ),
    ],
    ids=["no-cuprum-entry", "two-cuprum-entries"],
)
def test_lock_pin_requires_exactly_one_package_entry(lock_text: str) -> None:
    """A lock with no cuprum, or with two, cannot answer the question."""
    with pytest.raises(SelectionError):
        lock_pin(lock_text, site="test lock")


def test_lock_specifier_rejects_a_lock_whose_requirement_is_a_range() -> None:
    """A stale recorded requirement is reported as such, not as a version drift.

    This is the shape ``uv lock`` produces when it has not been re-run since
    ``pyproject.toml`` moved from a range to a pin: the resolved version can
    be right while the recorded requirement still admits every version.
    """
    lock_text = (
        "[[package]]\n"
        "name = 'rust-prover-tools'\n"
        "version = '0.1.0'\n"
        "[package.metadata]\n"
        "requires-dist = [{ name = 'cuprum', specifier = '>=0.1' }]\n"
    )

    with pytest.raises(SelectionError):
        lock_specifier(lock_text, site="test lock")


def test_every_site_declares_the_same_exact_cuprum_pin() -> None:
    """The declared, locked, recorded, and installed versions all agree.

    The expected value comes from ``pyproject.toml`` and nowhere else, so this
    states alignment rather than a particular version. Each assertion names
    its own site, because "cuprum is wrong somewhere" is not actionable: the
    fix differs for a requirement, a lock resolution, a lock record, and an
    installed distribution.
    """
    expected = declared_pin(PYPROJECT.read_text(encoding="utf-8"))

    lock = LOCKFILE.read_text(encoding="utf-8")
    assert lock_pin(lock, site="uv.lock") == expected, (
        f"uv.lock resolves cuprum to a version other than {expected}"
    )
    assert lock_specifier(lock, site="uv.lock") == expected, (
        f"uv.lock's recorded requirement disagrees with pyproject.toml ({expected})"
    )

    installed = installed_version()
    assert installed == expected, (
        f"the test interpreter has cuprum {installed}, not {expected}; run 'make build'"
    )


def _committed(path: Path) -> str:
    """Return the committed content of one tracked file.

    Parameters
    ----------
    path : Path
        The tracked file, expressed inside this repository.

    Returns
    -------
    str
        The file's content as of the index.

    Raises
    ------
    AssertionError
        If Git cannot produce the blob, so an environment problem is reported
        as such rather than as a stale lock.
    """
    relative = path.relative_to(ROOT).as_posix()
    completed = subprocess.run(
        [GIT_BINARY, "show", f":{relative}"],
        capture_output=True,
        text=True,
        check=False,
        cwd=ROOT,
        timeout=45,
    )
    assert completed.returncode == 0, (
        f"could not read the committed {relative} from the index; git said:\n"
        f"{completed.stderr}"
    )
    return completed.stdout


@pytest.mark.skipif(not (ROOT / ".git").exists(), reason=_NO_GIT_CHECKOUT)
def test_the_lock_is_fresh_as_committed(tmp_path: Path) -> None:
    """``uv.lock`` matches ``pyproject.toml`` as committed, not as built.

    The pair is written into a scratch directory and resolved there. That is
    what keeps this from being vacuous: ``make build`` runs ``uv sync``, which
    rewrites a stale ``uv.lock`` in place, so a check reading the working tree
    would assert a condition the gate had already repaired.
    """
    (tmp_path / PYPROJECT.name).write_text(_committed(PYPROJECT), encoding="utf-8")
    (tmp_path / LOCKFILE.name).write_text(_committed(LOCKFILE), encoding="utf-8")

    completed = subprocess.run(
        [UV_BINARY, "lock", "--check"],
        capture_output=True,
        text=True,
        check=False,
        cwd=tmp_path,
        timeout=45,
    )

    assert completed.returncode == 0, (
        "uv.lock is stale as committed; run 'uv lock'. uv said:\n"
        f"{completed.stdout}{completed.stderr}"
    )
