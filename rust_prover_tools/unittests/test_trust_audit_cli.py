"""Prove the trust audit command honours its output and status contract."""

from __future__ import annotations

import json
import typing as typ
from pathlib import Path

from rust_prover_tools.trust_audit_cli import audit_trust
from rust_prover_tools.unittests import conftest
from rust_prover_tools.unittests.trust_audit_tree_support import symlink, write

if typ.TYPE_CHECKING:
    import pytest


def test_a_text_report_names_each_item_and_counts_the_files(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Report each item as ``path:line:column: word`` and finish with a count."""
    write(tmp_path, {"src/lib.rs": "fn f() {\n    kani::assume(true);\n}"})

    status = audit_trust([tmp_path / "src/lib.rs"], repo_root=tmp_path)
    captured = capsys.readouterr()

    assert status == 0
    assert "src/lib.rs:2:11: assume\n" in captured.out
    assert "audited 1 file, 1 trusted item\n" in captured.out


def test_a_clean_audit_reports_no_items(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Report a proof that asks for no trust, and still name the file count."""
    write(tmp_path, {"src/lib.rs": "fn f() {}"})

    status = audit_trust([tmp_path / "src/lib.rs"], repo_root=tmp_path)
    captured = capsys.readouterr()

    assert status == 0
    assert "audited 1 file, 0 trusted items\n" in captured.out


def test_a_json_report_carries_each_item_and_its_location(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Emit the stable machine-readable shape."""
    write(tmp_path, {"src/lib.rs": "kani::assume(true);"})

    status = audit_trust(
        [tmp_path / "src/lib.rs"],
        repo_root=tmp_path,
        output_format="json",
    )
    payload = json.loads(capsys.readouterr().out)

    assert status == 0
    assert payload == {
        "schema_version": 1,
        "provers": ["kani", "verus"],
        "files_audited": 1,
        "items": [
            {"item": "assume", "path": "src/lib.rs", "line": 1, "column": 7},
        ],
    }


def test_a_missing_source_fails_with_a_diagnostic(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Fail the audit rather than report an empty result for unread source."""
    status = audit_trust([tmp_path / "src/lib.rs"], repo_root=tmp_path)
    captured = capsys.readouterr()

    assert status == 1
    assert "src/lib.rs cannot be read" in captured.err
    assert not captured.out


def test_a_source_outside_the_root_is_refused(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Refuse a driver the report could not name relative to its root."""
    repo = tmp_path / "repo"
    repo.mkdir()
    outside = tmp_path / "elsewhere.rs"
    outside.write_text("fn f() {}", encoding="utf-8")

    status = audit_trust([outside], repo_root=repo)
    captured = capsys.readouterr()

    assert status == 1
    assert "is outside the repository root" in captured.err


@conftest.SKIP_CHDIR_UNDER_MUTMUT
def test_a_relative_driver_is_read_from_the_repository_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Resolve a relative driver against the root, not the working directory.

    ``--repo-root R src/lib.rs`` names ``R/src/lib.rs`` from any directory, so
    a decoy beside the working directory must not be audited instead.
    """
    write(tmp_path, {"src/lib.rs": "kani::assume(true);"})
    elsewhere = tmp_path / "elsewhere"
    write(tmp_path, {"elsewhere/src/lib.rs": "fn decoy() {}"})
    monkeypatch.chdir(elsewhere)

    status = audit_trust([Path("src/lib.rs")], repo_root=tmp_path)
    captured = capsys.readouterr()

    assert status == 0
    assert "src/lib.rs:1:7: assume\n" in captured.out


def test_a_relative_driver_that_climbs_out_is_refused(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Refuse a relative driver whose resolution leaves the root."""
    status = audit_trust([Path("../outside.rs")], repo_root=tmp_path)
    captured = capsys.readouterr()

    assert status == 1
    assert "resolves outside the repository" in captured.err


def test_a_procedural_macro_scan_reads_string_literals(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Pass the toggle through to the scan."""
    write(tmp_path, {"src/lib.rs": 'fn f() { let _ = "kani::assume(x);"; }'})

    audit_trust(
        [tmp_path / "src/lib.rs"],
        repo_root=tmp_path,
        procedural_macro=True,
    )
    captured = capsys.readouterr()

    assert "src/lib.rs:1:25: assume\n" in captured.out


#: One file importing both provers, so each rule can be selected in isolation.
_BOTH_PROVERS: typ.Final[str] = "\n".join(
    (
        "use vstd::prelude::*;",
        "fn verus_side() { admit(); }",
        "fn kani_side() { kani::assume(true); }",
    ),
)


def test_the_default_selection_reports_every_prover(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Without ``--prover``, both vocabularies are applied."""
    write(tmp_path, {"src/lib.rs": _BOTH_PROVERS})

    status = audit_trust([tmp_path / "src/lib.rs"], repo_root=tmp_path)
    captured = capsys.readouterr()

    assert status == 0
    assert "provers: kani, verus\n" in captured.out
    assert "admit\n" in captured.out
    assert "assume\n" in captured.out


def test_selecting_kani_reports_no_verus_word(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A Kani-only audit ignores the Verus vocabulary, even in a importing file."""
    write(tmp_path, {"src/lib.rs": _BOTH_PROVERS})

    status = audit_trust(
        [tmp_path / "src/lib.rs"],
        repo_root=tmp_path,
        prover=["kani"],
    )
    captured = capsys.readouterr()

    assert status == 0
    assert "provers: kani\n" in captured.out
    assert ": assume\n" in captured.out
    assert "admit" not in captured.out


def test_selecting_verus_reports_no_kani_path(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A Verus-only audit ignores Kani's qualified paths."""
    write(tmp_path, {"src/lib.rs": _BOTH_PROVERS})

    status = audit_trust(
        [tmp_path / "src/lib.rs"],
        repo_root=tmp_path,
        prover=["verus"],
    )
    captured = capsys.readouterr()

    assert status == 0
    assert "provers: verus\n" in captured.out
    assert ": admit\n" in captured.out
    assert "assume" not in captured.out


def test_a_repeated_prover_is_reported_once(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Repeating a selection changes nothing and reports it once."""
    write(tmp_path, {"src/lib.rs": _BOTH_PROVERS})

    status = audit_trust(
        [tmp_path / "src/lib.rs"],
        repo_root=tmp_path,
        prover=["verus", "verus", "kani"],
        output_format="json",
    )
    payload = json.loads(capsys.readouterr().out)

    assert status == 0
    assert payload["provers"] == ["verus", "kani"]


def test_an_unknown_prover_is_refused(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A name the audit does not know fails rather than reporting silently."""
    write(tmp_path, {"src/lib.rs": "fn f() {}"})

    status = audit_trust(
        [tmp_path / "src/lib.rs"],
        repo_root=tmp_path,
        prover=["coq"],
    )
    captured = capsys.readouterr()

    assert status == 1
    assert "unknown prover 'coq'" in captured.err
    assert "known provers are kani, verus" in captured.err
    assert not captured.out


def test_the_root_alias_and_repeated_prover_parse(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The command line accepts ``--root`` and a repeated ``--prover``.

    The adapter's own parameters prove the behaviour; this test proves the
    spellings the user guide documents actually reach them.  The procedural
    toggle is cleared so an ambient value cannot change the parsed arguments.
    """
    from rust_prover_tools.cli import app

    monkeypatch.delenv("INPUT_PROCEDURAL_MACRO", raising=False)
    write(tmp_path, {"src/lib.rs": _BOTH_PROVERS})

    status = app(
        [
            "audit",
            "trust",
            "--root",
            str(tmp_path),
            "--prover",
            "verus",
            "--prover",
            "kani",
            "src/lib.rs",
        ],
        exit_on_error=False,
    )
    captured = capsys.readouterr()

    assert status == 0
    assert "provers: verus, kani\n" in captured.out
    assert ": admit\n" in captured.out
    assert ": assume\n" in captured.out


def test_an_outside_driver_symlinked_into_the_root_is_refused(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Refuse an outside spelling whose children would resolve elsewhere."""
    repo = tmp_path / "repo"
    write(repo, {"src/lib.rs": "kani::assume(true);"})
    outside = tmp_path / "outside.rs"
    symlink(outside, repo / "src/lib.rs")

    status = audit_trust([outside], repo_root=repo)
    captured = capsys.readouterr()

    assert status == 1
    assert "only through a symbolic link" in captured.err


def test_an_absolute_driver_through_a_symlink_names_the_compiled_file(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Accept an absolute driver and report the file it physically reaches."""
    repo = tmp_path / "repo"
    write(repo, {"src/lib.rs": "kani::assume(true);"})
    symlink(repo / "alias.rs", repo / "src/lib.rs")

    status = audit_trust([repo / "alias.rs"], repo_root=repo)
    captured = capsys.readouterr()

    assert status == 0
    assert "src/lib.rs:1:7: assume\n" in captured.out
