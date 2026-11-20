"""Prove the trust audit command honours its output and status contract."""

from __future__ import annotations

import json
import typing as typ

from rust_prover_tools.trust_audit_cli import audit_trust

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

    import pytest


def write(root: Path, files: cabc.Mapping[str, str]) -> None:
    """Write each source beneath ``root``, creating its parent directory."""
    for relative, text in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


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
    assert "audited 1 files, 1 trusted item\n" in captured.out


def test_a_clean_audit_reports_no_items(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Report a proof that asks for no trust, and still name the file count."""
    write(tmp_path, {"src/lib.rs": "fn f() {}"})

    status = audit_trust([tmp_path / "src/lib.rs"], repo_root=tmp_path)
    captured = capsys.readouterr()

    assert status == 0
    assert "audited 1 files, 0 trusted items\n" in captured.out


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
    outside = tmp_path.parent / "elsewhere.rs"
    outside.write_text("fn f() {}", encoding="utf-8")

    status = audit_trust([outside], repo_root=tmp_path / "repo")
    captured = capsys.readouterr()

    assert status == 1
    assert "is outside the repository root" in captured.err


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
