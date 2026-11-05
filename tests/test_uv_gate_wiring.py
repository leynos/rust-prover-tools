"""Contract: every uv call in the Makefile goes through the vendored helper.

The helper (``scripts/uv_gate.py``) is copied byte for byte from
leynos/shared-actions and audited by concordat's ``uv-gate-baseline`` rule.
These checks keep this repository's wiring to it from drifting between audits.
They ask GNU make what it would run (``make -nB`` with a marker standing in for
the helper) instead of pattern-matching recipe text.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MAKEFILE_PATH = ROOT / "Makefile"
MAKEFILE = MAKEFILE_PATH.read_text(encoding="utf-8") if MAKEFILE_PATH.exists() else ""
EXPECTED_PREPARE = "prepare --group dev"
EXPECTED_DEFAULT_GOAL = "all"
# The targets that call the helper: (target, tools it runs, runs the project, and
# whether make's own prerequisite graph must reach prepare; a target that reaches
# the project only through a recursive $(MAKE) is covered by that sub-target).
GATES: tuple[tuple[str, tuple[str, ...], bool, bool], ...] = (
    ("pytest", ("which",), True, True),
    ("lint", ("pylint", "ruff"), True, True),
    ("build-release", ("python",), True, True),
    ("lint-python", ("pylint", "ruff"), True, True),
    ("fmt", ("ruff",), True, True),
    ("typecheck", ("ty",), True, True),
    ("test-workflow-contracts", ("cv005-contracts",), False, False),
    ("all", ("cv005-contracts", "pylint", "pytest", "ruff", "ty", "which"), True, True),
    ("test", ("pytest", "which"), True, True),
    ("check-fmt", ("ruff",), True, True),
)
# The targets whose own recipe runs the helper's prepare.
PREPARE_TARGETS: tuple[str, ...] = ("prepare",)
# Every Makefile line that mentions uv without going through the helper. A new
# mention fails the test until it is reviewed and added here.
ALLOWED_UV_LINES: tuple[str, ...] = (
    "$(error UV=$(UV) is not an executable file)",
    "$(error UV=$(UV) must be named uv)",
    "$(error UV=$(UV) must be uv or a path to an executable file named uv)",
    '@command -v $(UV) >/dev/null 2>&1 || { printf "Error: uv is required, but \'%s\' was not found or is not executable\\n" "$(UV)" >&2; exit 1; }',  # ruff: ignore[line-too-long]
    'TYPOS_CONFIG_BUILDER = $(UV) tool run --python 3.14 --from "git+https://github.com/leynos/typos-config-builder.git@$(TYPOS_CONFIG_BUILDER_VERSION)" typos-config-builder',  # ruff: ignore[line-too-long]
    "UV ?= $(shell command -v uv 2>/dev/null || printf '%s/.local/bin/uv' \"$$HOME\")",
    "export PATH := $(if $(findstring /,$(UV)),$(patsubst %/,%,$(dir $(UV))):)$(PATH)",
    "ifeq ($(findstring /,$(UV)),)",
    "ifeq ($(shell test -f '$(UV)' -a -x '$(UV)' && printf y),)",
    "ifneq ($(UV),uv)",
    "ifneq ($(notdir $(UV)),uv)",
)
MAKE = shutil.which("make")
MARKER = "UV-GATE-MARKER"
NEEDS_MAKE = pytest.mark.skipif(MAKE is None, reason="make is not installed")
pytestmark = pytest.mark.skipif(not MAKEFILE, reason="no Makefile in this workspace")
UV_WORD = re.compile(r"(?<![A-Za-z0-9_-])uvx?(?![A-Za-z0-9_-])|\$[({]UV[)}]")
COMMENT = re.compile(r"(?<!\\)(?:^|\s)#.*$")
SEGMENT_SPLIT = re.compile(r"&&|\|\||[;|]")
XARGS_VALUED = {"-n", "-I", "-P", "-L", "-s", "-d", "-E", "-a"}
LEADING_WORDS = {"if", "then", "else", "elif", "do", "!", "time", "exec", "env"}
GATE_ASSIGNMENT = re.compile(
    r"^\s*(?:(?:override|export)\s+)*UV_GATE\s*[:?+!]*=", re.MULTILINE
)
CACHE_OVERRIDE = re.compile(r"UV_(?:CACHE|TOOL)_DIR\s*(?:[:?+]{0,2}=|:)")


def uv_mentions(makefile: str) -> list[str]:
    """Return the normalised Makefile lines that mention uv, comments removed."""
    lines = (
        COMMENT.sub("", line) for line in makefile.replace("\\\n", " ").splitlines()
    )
    return [" ".join(line.split()) for line in lines if UV_WORD.search(line)]


def is_prefix(word: str) -> bool:
    """Say whether ``word`` only prefixes the command that follows it."""
    bare = word.lstrip("@+-")
    return not bare or bare in LEADING_WORDS or bool(re.fullmatch(r"\w+=\S*", word))


def skip_xargs(words: list[str]) -> list[str]:
    """Drop a leading ``xargs`` and its flags, leaving the command it runs."""
    if words[:1] != ["xargs"]:
        return words
    rest = words[1:]
    while rest and rest[0].startswith("-"):
        flag = rest.pop(0)
        if flag in XARGS_VALUED and rest:
            rest.pop(0)
    return rest


def command_words(segment: str) -> list[str]:
    """Return the words of one shell segment from its command word onwards."""
    words = segment.replace("(", " ").replace("{", " ").split()
    while words and is_prefix(words[0]):
        words = words[1:]
    return skip_xargs(words)


def helper_calls(printed: str) -> list[str]:
    """Return the commands in make's printed recipes that run the marker."""
    segments = (
        segment
        for line in printed.splitlines()
        for segment in SEGMENT_SPLIT.split(COMMENT.sub("", line))
    )
    commands = (command_words(segment) for segment in segments)
    return [
        " ".join(words).lstrip("@+-")
        for words in commands
        if words and words[0].lstrip("@+-") == MARKER
    ]


def tool_word(call: str) -> str:
    """Return the first word after `--` in a helper run or tool call, if any."""
    subcommand = call.split()[1]
    _, separator, rest = call.partition(" -- ")
    words = rest.split()
    return words[0] if subcommand in {"run", "tool"} and separator and words else ""


def tools_run(calls: list[str]) -> set[str]:
    """Return the tools the helper runs in ``calls``."""
    return {word for word in map(tool_word, calls) if word}


def database_prerequisites() -> dict[str, set[str]]:
    """Return make's rule database as a target to prerequisites map."""
    rules: dict[str, set[str]] = {}
    for line in make("-qp", "--", ok=(0, 1)).stdout.splitlines():
        rule = re.match(r"^([^\s#:=%][^:=%]*):(?![:=])([^|;]*)", line)
        if rule:
            rules.setdefault(rule[1].strip(), set()).update(rule[2].split())
    return rules


def reaches(name: str, rules: dict[str, set[str]], goals: tuple[str, ...]) -> bool:
    """Say whether ``name`` is, or depends transitively on, one of ``goals``."""
    pending, seen = [name], set()
    while pending:
        current = pending.pop()
        if current in goals:
            return True
        if current not in seen:
            seen.add(current)
            pending.extend(rules.get(current, ()))
    return False


def make(
    *arguments: str, ok: tuple[int, ...] = (0,)
) -> subprocess.CompletedProcess[str]:
    """Run GNU make in the repository root; fail on an unexpected exit status."""
    environment = {k: v for k, v in os.environ.items() if k != "UV_GATE"}
    done = subprocess.run(
        [MAKE or "make", "--no-print-directory", "-f", "Makefile", *arguments],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert done.returncode in ok, (
        f"make {arguments} exited {done.returncode}: {done.stderr}"
    )
    return done


@NEEDS_MAKE
def test_the_gate_variable_is_assigned_once_to_the_helper() -> None:
    """UV_GATE has one assignment, and make's effective value is the helper."""
    assignments = GATE_ASSIGNMENT.findall(MAKEFILE)
    database = make("-qp", "--", ok=(0, 1)).stdout

    assert len(assignments) == 1, f"UV_GATE is assigned {len(assignments)} times"
    assert re.search(
        r"^UV_GATE = python3 scripts/uv_gate\.py$", database, re.MULTILINE
    ), "make's effective UV_GATE must be the helper command"


def test_no_uv_cache_override_remains() -> None:
    """Neither the Makefile nor the workflows set a uv cache or tool directory."""
    workflows = [p.read_text(encoding="utf-8") for p in ROOT.glob(".github/**/*.y*ml")]

    offenders = [t[:40] for t in [MAKEFILE, *workflows] if CACHE_OVERRIDE.search(t)]
    assert not offenders, f"a uv cache location is overridden: {offenders}"


def test_every_remaining_mention_of_uv_is_reviewed() -> None:
    """A uv mention that is not routed through the helper must be on the list."""
    unreviewed = [
        line for line in uv_mentions(MAKEFILE) if line not in ALLOWED_UV_LINES
    ]

    assert not unreviewed, f"unreviewed uv mentions (use the helper): {unreviewed}"


@pytest.mark.parametrize(
    "line",
    [
        "\tuv run pytest",
        "RUNNER = uvx ruff check",
        "RUNNER = uv --directory . run pytest",
        "\t${UV} run pytest",
        "\t$(UV) --offline run pytest",
        "\t'/usr/bin/uv' run pytest",
        "\tenv uvx ruff check",
        "\ttrue && uv lock",
        "\tuv sync  # $(UV_GATE) run --",
        "ifeq ($(shell command -v uv),)",
    ],
)
def test_the_scan_finds_a_mention(line: str) -> None:
    """Every spelling of a direct uv call is found."""
    assert uv_mentions(f"check:\n{line}\n")


@pytest.mark.parametrize(
    "line",
    [
        "\t$(UV_GATE) run -- pytest",
        "\t$(UV_GATE) tool --from ruff@0.9.0 -- ruff check",
        "# uv run is described here",
        "\t@echo done  # uv is not needed",
    ],
)
def test_the_scan_ignores_gated_calls_and_comments(line: str) -> None:
    """The helper's own spellings and comments are not mentions."""
    assert not uv_mentions(f"check:\n{line}\n")


@pytest.mark.parametrize(
    ("printed", "expected"),
    [
        (f"{MARKER} prepare --group dev", [f"{MARKER} prepare --group dev"]),
        (f"@{MARKER} run -- pytest -q", [f"{MARKER} run -- pytest -q"]),
        (f"echo {MARKER} prepare --group dev", []),
        (f"# {MARKER} run -- pytest", []),
        (f"true && {MARKER} run -- pytest", [f"{MARKER} run -- pytest"]),
        (f"echo hi; {MARKER} tool --from x@1 -- x", [f"{MARKER} tool --from x@1 -- x"]),
        (f"if ! {MARKER} run -- x; then echo no; fi", [f"{MARKER} run -- x"]),
    ],
)
def test_the_printed_recipe_parser_finds_only_executed_helper_calls(
    printed: str, expected: list[str]
) -> None:
    """Only a helper call in command position counts, not echoed or commented text."""
    assert helper_calls(printed) == expected


@NEEDS_MAKE
def test_prepare_is_phony_and_runs_the_helper_prepare() -> None:
    """`make prepare` is phony and executes exactly the expected helper command."""
    printed = make("-nB", "prepare", f"UV_GATE={MARKER}").stdout

    assert re.search(r"^\.PHONY:.*\bprepare\b", MAKEFILE, re.MULTILINE), (
        "prepare must be phony"
    )
    assert f"{MARKER} {EXPECTED_PREPARE}" in helper_calls(printed), (
        f"make prepare must run `{EXPECTED_PREPARE}` as a command: {printed!r}"
    )


@NEEDS_MAKE
@pytest.mark.parametrize(("target", "tools", "runs_project", "graph"), GATES)
def test_a_gate_runs_its_tools_prepared_and_separated(
    target: str, tools: tuple[str, ...], *, runs_project: bool, graph: bool
) -> None:
    """Each gate runs its tools through the helper, prepared first, with `--`."""
    printed = make("-nB", target, f"UV_GATE={MARKER}").stdout
    calls = helper_calls(printed)
    subcommands = [call.split()[1] for call in calls]
    prepare = f"{MARKER} {EXPECTED_PREPARE}"

    assert set(tools) <= tools_run(calls), (
        f"make {target} must run {tools} through the helper, ran {tools_run(calls)}"
    )
    if runs_project:
        assert "run" in subcommands, f"make {target} no longer runs the project"
        first_run = subcommands.index("run")
        assert prepare in calls[:first_run], (
            f"make {target} must prepare before it runs the project: {calls}"
        )
    unseparated = [
        c for c in calls if c.split()[1] in {"run", "tool"} and " -- " not in c
    ]
    assert not unseparated, f"helper calls without `--`: {unseparated}"


@NEEDS_MAKE
@pytest.mark.parametrize(
    "target", [name for name, _, runs_project, graph in GATES if runs_project and graph]
)
def test_a_project_gate_depends_on_prepare(target: str) -> None:
    """Make's own prerequisite graph orders prepare first, even under `make -j`."""
    rules = database_prerequisites()

    assert reaches(target, rules, PREPARE_TARGETS), (
        f"{target} must depend, directly or not, on one of {PREPARE_TARGETS}"
    )


def test_a_failing_helper_call_is_not_ignored() -> None:
    """No recipe line tolerates a failing helper call."""
    ignored = re.compile(
        r"^\t[@+]*-\s*\$\(UV_GATE\)|\$\(UV_GATE\)[^\n]*\|\|\s*(?:true|:)\s*$"
    )
    offenders = [line.strip() for line in MAKEFILE.splitlines() if ignored.search(line)]

    assert not offenders, f"helper failures are ignored: {offenders}"


@NEEDS_MAKE
def test_the_default_goal_is_unchanged() -> None:
    """Adding `prepare` must not change what a bare `make` runs."""
    database = make("-qp", "--", ok=(0, 1)).stdout

    assert f".DEFAULT_GOAL := {EXPECTED_DEFAULT_GOAL}" in database, (
        f"a bare make must still run {EXPECTED_DEFAULT_GOAL}"
    )


@NEEDS_MAKE
@NEEDS_MAKE
def test_a_selected_uv_leads_the_exported_path(tmp_path: Path) -> None:
    """`make UV=/dir/uv` hands recipes a PATH that starts with that directory."""
    fake = tmp_path / "bin" / "uv"
    fake.parent.mkdir()
    fake.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake.chmod(0o755)
    probe = ".PHONY: __uv_path\n__uv_path:\n\t@printf '%s' \"$$PATH\""
    done = make(f"UV={fake}", f"--eval={probe}", "__uv_path")

    assert done.stdout.startswith(f"{fake.parent}:"), (
        f"recipes must see the selected uv first on PATH, got {done.stdout!r}"
    )


@NEEDS_MAKE
@pytest.mark.parametrize("kind", ["absent", "not-executable", "misnamed", "slashless"])
def test_an_unusable_selected_uv_is_refused(tmp_path: Path, kind: str) -> None:
    """A UV that is absent, not executable or not named uv must not be used."""
    chosen = tmp_path / ("uv-0.11" if kind == "misnamed" else "uv")
    if kind == "slashless":
        chosen = Path("uv-custom")
    elif kind != "absent":
        chosen.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        chosen.chmod(0o755 if kind == "misnamed" else 0o644)
    probe = ".PHONY: __uv_path\n__uv_path:\n\t@true"
    done = make(f"UV={chosen}", f"--eval={probe}", "__uv_path", ok=(2,))

    assert "UV=" in done.stderr, done.stderr
