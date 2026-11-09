"""Contract tests for the local setup receipt of a snapshot generation."""

from __future__ import annotations

import dataclasses as dc
import json
import typing as typ

import pytest

from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_snapshot.identity import (
    build_identity_report,
    runtime_for,
    snapshot_layout,
)
from rust_prover_tools.kani_snapshot.models import (
    AssetRecord,
    BackendIdentity,
    BuildInputs,
    CacheObservation,
    ResourceObservation,
    RuntimeIdentity,
    SnapshotManifest,
    SnapshotPin,
    ToolchainIdentity,
    WorkflowIdentity,
)
from rust_prover_tools.kani_snapshot.receipt import (
    ObservedToolchain,
    SetupEvidence,
    SetupReceipt,
    build_receipt,
    load_receipt,
    parse_receipt,
    render_receipt,
    validate_receipt,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

    from rust_prover_tools.kani_snapshot.identity import SnapshotLayout

_SHA = "a" * 64
_GENERATION = "g1"


def _pin() -> SnapshotPin:
    """Return one validated pin."""
    return SnapshotPin(
        schema_version=1,
        repository="leynos/rust-prover-tools",
        tag="kani-snapshot-2026-11-0123456789ab-r1",
        upstream_commit="b" * 40,
        builder_commit="c" * 40,
        target="x86_64-unknown-linux-gnu",
        manifest_sha256=_SHA,
    )


def _manifest() -> SnapshotManifest:
    """Return a complete manifest whose assets a receipt can record."""
    return SnapshotManifest(
        schema_version=1,
        repository="leynos/rust-prover-tools",
        tag="kani-snapshot-2026-11-0123456789ab-r1",
        upstream_commit="b" * 40,
        builder_commit="c" * 40,
        target="x86_64-unknown-linux-gnu",
        workflow=WorkflowIdentity(
            path=".github/workflows/kani-monthly.yml",
            event="workflow_dispatch",
            ref="refs/heads/main",
            run_id=1,
            run_attempt=1,
        ),
        toolchain=ToolchainIdentity(
            identifier="nightly-2026-09-23",
            rustc_vv="rustc 1.100.0-nightly (abc 2026-09-23)",
            kani_compiler="kani-compiler 0.66.0",
        ),
        launcher_package_version="0.66.0",
        backend=BackendIdentity(version="0.66.0", revision="b" * 40),
        runtime=RuntimeIdentity(glibc_baseline="2.34"),
        build=BuildInputs(submodules=(), lock_digests=(), recipe_digest="e" * 64),
        prerequisites=(),
        cache=CacheObservation(outcome="miss"),
        resources=ResourceObservation(
            wall_seconds=0.0,
            disk_high_water_bytes=0,
            asset_bytes=0,
        ),
        assets=(
            AssetRecord(
                role="backend",
                name="kani-backend.tar.gz",
                media_type="application/gzip",
                size=10,
                sha256="1" * 64,
            ),
            AssetRecord(
                role="launcher",
                name="kani-launcher.tar.gz",
                media_type="application/gzip",
                size=20,
                sha256="2" * 64,
            ),
            AssetRecord(
                role="notices",
                name="notices.tar.gz",
                media_type="application/gzip",
                size=30,
                sha256="3" * 64,
            ),
            AssetRecord(
                role="inventory",
                name="inventory.json",
                media_type="application/json",
                size=40,
                sha256="4" * 64,
            ),
        ),
    )


def _case(
    tmp_path: Path,
) -> tuple[SnapshotPin, SnapshotManifest, SnapshotLayout, SetupReceipt]:
    """Return pin, manifest, layout and a valid receipt for one generation."""
    pin = _pin()
    manifest = _manifest()
    layout = snapshot_layout(pin, cache_home=tmp_path, generation=_GENERATION)
    runtime = runtime_for(layout, manifest)
    identity = build_identity_report(pin, manifest, runtime)
    receipt = build_receipt(
        pin,
        manifest,
        identity,
        SetupEvidence(
            observed=ObservedToolchain(
                rustc_vv="rustc 1.100.0-nightly (abc 2026-09-23)",
                kani_compiler="kani-compiler 0.66.0",
            ),
            toolchain_link_target=layout.rustup_home
            / "toolchains"
            / manifest.toolchain.identifier,
        ),
    )
    return pin, manifest, layout, receipt


def test_receipt_round_trips_through_canonical_json(tmp_path: Path) -> None:
    """Exact rendered bytes parse back to an equal receipt."""
    _, _, _, receipt = _case(tmp_path)

    data = render_receipt(receipt)

    assert data.endswith(b"\n")
    assert parse_receipt(data) == receipt
    assert render_receipt(parse_receipt(data)) == data


def test_receipt_records_the_complete_asset_set(tmp_path: Path) -> None:
    """The receipt binds every manifest payload by name, size and digest."""
    _, manifest, _, receipt = _case(tmp_path)

    assert [(asset.name, asset.size, asset.sha256) for asset in receipt.assets] == [
        (asset.name, asset.size, asset.sha256) for asset in manifest.assets
    ]


def test_receipt_records_observed_toolchain_identity(tmp_path: Path) -> None:
    """Observed `rustc -vV` and compiler text are recorded, not inferred."""
    _, _, _, receipt = _case(tmp_path)

    assert receipt.observed.rustc_vv.startswith("rustc 1.100.0-nightly")
    assert receipt.observed.kani_compiler == "kani-compiler 0.66.0"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda doc: {**doc, "extra": True},
        lambda doc: {key: value for key, value in doc.items() if key != "tag"},
        lambda doc: {**doc, "schema_version": 2},
        lambda doc: {**doc, "schema_version": True},
        lambda doc: {**doc, "observed": {**doc["observed"], "extra": 1}},
        lambda doc: {**doc, "assets": {}},
        lambda doc: {
            **doc,
            "assets": [{"name": "a", "sha256": _SHA, "size": 1, "x": 1}],
        },
        lambda doc: {**doc, "generation": "relative/path"},
        lambda doc: {**doc, "toolchain_link_target": "relative"},
        lambda doc: {**doc, "tag": 7},
    ],
    ids=[
        "unknown-top-key",
        "missing-key",
        "wrong-schema",
        "boolean-schema",
        "unknown-nested-key",
        "assets-not-array",
        "unknown-asset-key",
        "relative-generation",
        "relative-link-target",
        "wrong-tag-type",
    ],
)
def test_invalid_receipts_are_rejected(
    tmp_path: Path, mutate: cabc.Callable[[typ.Any], typ.Any]
) -> None:
    """Every structural deviation fails closed."""
    _, _, _, receipt = _case(tmp_path)
    document = json.loads(render_receipt(receipt))

    with pytest.raises(ProverToolError, match="receipt"):
        parse_receipt(json.dumps(mutate(document)).encode())


def test_duplicate_receipt_keys_are_rejected(tmp_path: Path) -> None:
    """A repeated key may not silently win over the first value."""
    _, _, _, receipt = _case(tmp_path)
    data = render_receipt(receipt).replace(b"{", b'{"schema_version":1,', 1)

    with pytest.raises(ProverToolError, match="duplicate"):
        parse_receipt(data)


def test_validate_receipt_accepts_the_installed_generation(tmp_path: Path) -> None:
    """A receipt matching the pin, manifest and layout validates."""
    pin, manifest, layout, receipt = _case(tmp_path)

    validate_receipt(receipt, pin, manifest, layout)


def test_validate_receipt_rejects_a_different_manifest(tmp_path: Path) -> None:
    """A receipt for another identity cannot validate this generation."""
    pin, manifest, layout, receipt = _case(tmp_path)
    other = dc.replace(manifest, tag=pin.tag + "-other")

    with pytest.raises(ProverToolError, match="receipt"):
        validate_receipt(receipt, pin, other, layout)


def test_validate_receipt_rejects_a_changed_asset(tmp_path: Path) -> None:
    """A receipt whose asset set differs from the manifest is corrupt."""
    pin, manifest, layout, receipt = _case(tmp_path)
    changed = dc.replace(
        receipt,
        assets=(dc.replace(receipt.assets[0], sha256="9" * 64), *receipt.assets[1:]),
    )

    with pytest.raises(ProverToolError, match="asset"):
        validate_receipt(changed, pin, manifest, layout)


def test_validate_receipt_rejects_paths_outside_the_generation(
    tmp_path: Path,
) -> None:
    """Every recorded path must stay inside the generation it describes."""
    pin, manifest, layout, receipt = _case(tmp_path)
    escaped = dc.replace(receipt, generation=tmp_path / "elsewhere")

    with pytest.raises(ProverToolError, match="generation"):
        validate_receipt(escaped, pin, manifest, layout)


def test_validate_receipt_rejects_a_changed_toolchain_link(
    tmp_path: Path,
) -> None:
    """Setup's absolute toolchain symlink must name the pinned toolchain."""
    pin, manifest, layout, receipt = _case(tmp_path)
    changed = dc.replace(
        receipt,
        toolchain_link_target=layout.rustup_home / "toolchains" / "stable",
    )

    with pytest.raises(ProverToolError, match="toolchain"):
        validate_receipt(changed, pin, manifest, layout)


def test_load_receipt_reports_missing_and_invalid_files(tmp_path: Path) -> None:
    """A missing or malformed receipt is a local cache failure."""
    with pytest.raises(ProverToolError, match="receipt"):
        load_receipt(tmp_path / "missing.json")
    bad = tmp_path / "bad.json"
    bad.write_text("{}", encoding="utf-8")
    with pytest.raises(ProverToolError, match="receipt"):
        load_receipt(bad)
