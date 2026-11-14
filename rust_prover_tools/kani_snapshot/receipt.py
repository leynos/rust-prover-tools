"""Canonical rendering, parsing and validation of setup receipts.

The receipt binds the reviewed pin and manifest to the paths setup actually
produced, so cache reuse can reject a missing, renamed or replaced toolchain
before any proof runs. The frozen shapes live in `receipt_model`.
"""

from __future__ import annotations

import json
import typing as typ
from pathlib import Path

from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_snapshot.models import SCHEMA_VERSION
from rust_prover_tools.kani_snapshot.policy import is_safe_asset_name
from rust_prover_tools.kani_snapshot.receipt_model import (
    ObservedToolchain,
    ReceiptAsset,
    SetupEvidence,
    SetupReceipt,
)
from rust_prover_tools.kani_snapshot.strict_json import strict_json_loads
from rust_prover_tools.kani_snapshot.validation import (
    require_int,
    require_mapping,
    require_non_negative_int,
    require_sequence,
    require_sha256,
    require_text,
    require_truthy_text,
)

if typ.TYPE_CHECKING:
    from rust_prover_tools.kani_snapshot.identity import (
        SnapshotIdentity,
        SnapshotLayout,
    )
    from rust_prover_tools.kani_snapshot.models import (
        SnapshotManifest,
        SnapshotPin,
    )

__all__ = (
    "ObservedToolchain",
    "ReceiptAsset",
    "SetupEvidence",
    "SetupReceipt",
    "build_receipt",
    "load_receipt",
    "parse_receipt",
    "render_receipt",
    "validate_receipt",
)

_RECEIPT_KEYS: typ.Final[frozenset[str]] = frozenset({
    "schema_version",
    "tag",
    "upstream_commit",
    "builder_commit",
    "manifest_sha256",
    "target",
    "backend_version",
    "toolchain",
    "rustc_vv",
    "kani_compiler",
    "generation",
    "kani_home",
    "rustup_home",
    "cargo_home",
    "launcher",
    "driver",
    "compiler",
    "rustc",
    "toolchain_link_target",
    "observed",
    "assets",
})
_OBSERVED_KEYS: typ.Final[frozenset[str]] = frozenset({"rustc_vv", "kani_compiler"})
_ASSET_KEYS: typ.Final[frozenset[str]] = frozenset({"name", "size", "sha256"})
_PATH_FIELDS: typ.Final[tuple[str, ...]] = (
    "generation",
    "kani_home",
    "rustup_home",
    "cargo_home",
    "launcher",
    "driver",
    "compiler",
    "rustc",
    "toolchain_link_target",
)


def build_receipt(
    pin: SnapshotPin,
    manifest: SnapshotManifest,
    identity: SnapshotIdentity,
    evidence: SetupEvidence,
) -> SetupReceipt:
    """Assemble the receipt for a generation that just validated."""
    return SetupReceipt(
        schema_version=SCHEMA_VERSION,
        tag=pin.tag,
        upstream_commit=pin.upstream_commit,
        builder_commit=pin.builder_commit,
        manifest_sha256=pin.manifest_sha256,
        target=pin.target,
        backend_version=manifest.backend.version,
        toolchain=manifest.toolchain.identifier,
        rustc_vv=manifest.toolchain.rustc_vv,
        kani_compiler=manifest.toolchain.kani_compiler,
        generation=identity.generation,
        kani_home=identity.generation / "kani-home",
        rustup_home=identity.generation / "rustup-home",
        cargo_home=identity.generation / "cargo-home",
        launcher=identity.launcher,
        driver=identity.driver,
        compiler=identity.compiler,
        rustc=identity.rustc,
        toolchain_link_target=evidence.toolchain_link_target,
        observed=evidence.observed,
        assets=tuple(
            ReceiptAsset(name=asset.name, size=asset.size, sha256=asset.sha256)
            for asset in manifest.assets
        ),
    )


def render_receipt(receipt: SetupReceipt) -> bytes:
    """Render canonical receipt JSON with a final newline."""
    document = {
        "schema_version": receipt.schema_version,
        "tag": receipt.tag,
        "upstream_commit": receipt.upstream_commit,
        "builder_commit": receipt.builder_commit,
        "manifest_sha256": receipt.manifest_sha256,
        "target": receipt.target,
        "backend_version": receipt.backend_version,
        "toolchain": receipt.toolchain,
        "rustc_vv": receipt.rustc_vv,
        "kani_compiler": receipt.kani_compiler,
        "generation": str(receipt.generation),
        "kani_home": str(receipt.kani_home),
        "rustup_home": str(receipt.rustup_home),
        "cargo_home": str(receipt.cargo_home),
        "launcher": str(receipt.launcher),
        "driver": str(receipt.driver),
        "compiler": str(receipt.compiler),
        "rustc": str(receipt.rustc),
        "toolchain_link_target": str(receipt.toolchain_link_target),
        "observed": {
            "rustc_vv": receipt.observed.rustc_vv,
            "kani_compiler": receipt.observed.kani_compiler,
        },
        "assets": [
            {"name": asset.name, "size": asset.size, "sha256": asset.sha256}
            for asset in receipt.assets
        ],
    }
    encoded = json.dumps(
        document,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return (encoded + "\n").encode("ascii")


def parse_receipt(data: bytes) -> SetupReceipt:
    """Parse exact receipt bytes into a validated model."""
    document = require_mapping(strict_json_loads(data, "receipt"), "receipt document")
    _require_exact_keys(document, _RECEIPT_KEYS, "receipt")
    version = require_int(document["schema_version"], "receipt schema_version")
    if version != SCHEMA_VERSION:
        msg = f"snapshot receipt schema_version must be {SCHEMA_VERSION}"
        raise ProverToolError(msg)
    paths = {
        field: _require_path(document[field], f"receipt {field}")
        for field in _PATH_FIELDS
    }
    return SetupReceipt(
        schema_version=version,
        tag=require_truthy_text(document["tag"], "receipt tag"),
        upstream_commit=require_text(
            document["upstream_commit"], "receipt upstream_commit"
        ),
        builder_commit=require_text(
            document["builder_commit"], "receipt builder_commit"
        ),
        manifest_sha256=require_sha256(
            document["manifest_sha256"], "receipt manifest_sha256"
        ),
        target=require_truthy_text(document["target"], "receipt target"),
        backend_version=require_truthy_text(
            document["backend_version"], "receipt backend_version"
        ),
        toolchain=require_truthy_text(document["toolchain"], "receipt toolchain"),
        rustc_vv=require_truthy_text(document["rustc_vv"], "receipt rustc_vv"),
        kani_compiler=require_truthy_text(
            document["kani_compiler"], "receipt kani_compiler"
        ),
        observed=_parse_observed(document["observed"]),
        assets=_parse_assets(document["assets"]),
        generation=paths["generation"],
        kani_home=paths["kani_home"],
        rustup_home=paths["rustup_home"],
        cargo_home=paths["cargo_home"],
        launcher=paths["launcher"],
        driver=paths["driver"],
        compiler=paths["compiler"],
        rustc=paths["rustc"],
        toolchain_link_target=paths["toolchain_link_target"],
    )


def _require_exact_keys(
    document: dict[str, object],
    allowed: frozenset[str],
    field: str,
) -> None:
    """Require exactly the permitted key set."""
    unknown = sorted(set(document) - allowed)
    if unknown:
        msg = f"snapshot {field} has unknown keys: {', '.join(unknown)}"
        raise ProverToolError(msg)
    missing = sorted(allowed - set(document))
    if missing:
        msg = f"snapshot {field} is missing keys: {', '.join(missing)}"
        raise ProverToolError(msg)


def _require_path(value: object, field: str) -> Path:
    """Require an absolute path recorded as text."""
    path = Path(require_truthy_text(value, field))
    if not path.is_absolute():
        msg = f"snapshot {field} must be absolute: {path}"
        raise ProverToolError(msg)
    return path


def _parse_observed(value: object) -> ObservedToolchain:
    """Parse the observed compiler identity object."""
    document = require_mapping(value, "receipt observed")
    _require_exact_keys(document, _OBSERVED_KEYS, "receipt observed")
    return ObservedToolchain(
        rustc_vv=require_truthy_text(document["rustc_vv"], "receipt observed rustc_vv"),
        kani_compiler=require_truthy_text(
            document["kani_compiler"], "receipt observed kani_compiler"
        ),
    )


def _parse_assets(value: object) -> tuple[ReceiptAsset, ...]:
    """Parse the complete payload set, rejecting duplicates and unsafe names."""
    raw_assets = require_sequence(value, "receipt assets")
    assets: list[ReceiptAsset] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_assets):
        field = f"receipt assets[{index}]"
        document = require_mapping(raw, field)
        _require_exact_keys(document, _ASSET_KEYS, field)
        name = require_truthy_text(document["name"], f"{field} name")
        if not is_safe_asset_name(name):
            msg = f"snapshot {field} name is unsafe: {name!r}"
            raise ProverToolError(msg)
        if name in seen:
            msg = f"snapshot receipt lists asset '{name}' more than once"
            raise ProverToolError(msg)
        seen.add(name)
        assets.append(
            ReceiptAsset(
                name=name,
                size=require_non_negative_int(document["size"], f"{field} size"),
                sha256=require_sha256(document["sha256"], f"{field} sha256"),
            )
        )
    return tuple(assets)


def load_receipt(path: Path) -> SetupReceipt:
    """Read and parse a receipt file."""
    try:
        data = path.read_bytes()
    except OSError as exc:
        msg = f"failed to read snapshot receipt '{path}': {exc}"
        raise ProverToolError(msg) from exc
    return parse_receipt(data)


def validate_receipt(
    receipt: SetupReceipt,
    pin: SnapshotPin,
    manifest: SnapshotManifest,
    layout: SnapshotLayout,
) -> None:
    """Require a receipt to describe exactly this pin, manifest and layout."""
    _check_identity(receipt, pin, manifest)
    _check_paths(receipt, manifest, layout)
    _check_assets(receipt, manifest)


def _check_identity(
    receipt: SetupReceipt,
    pin: SnapshotPin,
    manifest: SnapshotManifest,
) -> None:
    """Compare the receipt's identity fields with the reviewed records."""
    expected: tuple[tuple[str, object, object], ...] = (
        ("tag", receipt.tag, pin.tag),
        ("manifest tag", receipt.tag, manifest.tag),
        ("upstream_commit", receipt.upstream_commit, pin.upstream_commit),
        ("builder_commit", receipt.builder_commit, pin.builder_commit),
        ("manifest_sha256", receipt.manifest_sha256, pin.manifest_sha256),
        ("target", receipt.target, pin.target),
        ("backend_version", receipt.backend_version, manifest.backend.version),
        ("toolchain", receipt.toolchain, manifest.toolchain.identifier),
        ("rustc_vv", receipt.rustc_vv, manifest.toolchain.rustc_vv),
        ("kani_compiler", receipt.kani_compiler, manifest.toolchain.kani_compiler),
    )
    for name, actual, wanted in expected:
        if actual != wanted:
            msg = f"snapshot receipt {name} does not match the reviewed snapshot"
            raise ProverToolError(msg)


def _check_paths(
    receipt: SetupReceipt,
    manifest: SnapshotManifest,
    layout: SnapshotLayout,
) -> None:
    """Require every recorded path to be the layout's expected path."""
    if receipt.generation != layout.generation_dir:
        msg = "snapshot receipt generation does not match the layout"
        raise ProverToolError(msg)
    kani_dir = layout.kani_home / f"kani-{manifest.backend.version}"
    toolchain_dir = layout.rustup_home / "toolchains" / manifest.toolchain.identifier
    expected = {
        "kani_home": layout.kani_home,
        "rustup_home": layout.rustup_home,
        "cargo_home": layout.cargo_home,
        "launcher": kani_dir / "bin" / "kani",
        "driver": kani_dir / "bin" / "kani-driver",
        "compiler": kani_dir / "bin" / "kani-compiler",
        "rustc": toolchain_dir / "bin" / "rustc",
        "toolchain_link_target": toolchain_dir,
    }
    for name, wanted in expected.items():
        if getattr(receipt, name) != wanted:
            msg = f"snapshot receipt {name} does not match the layout"
            raise ProverToolError(msg)
    if not receipt.toolchain_link_target.is_relative_to(
        layout.rustup_home / "toolchains"
    ):
        msg = "snapshot receipt toolchain link is outside the private toolchains"
        raise ProverToolError(msg)
    for path in (receipt.generation, receipt.kani_home, receipt.rustup_home):
        if not path.is_relative_to(layout.generation_dir):
            msg = "snapshot receipt path escapes its generation"
            raise ProverToolError(msg)


def _check_assets(receipt: SetupReceipt, manifest: SnapshotManifest) -> None:
    """Require the recorded payload set to match the manifest exactly."""
    expected = tuple(
        (asset.name, asset.size, asset.sha256) for asset in manifest.assets
    )
    actual = tuple((asset.name, asset.size, asset.sha256) for asset in receipt.assets)
    if actual != expected:
        msg = "snapshot receipt asset set does not match the manifest"
        raise ProverToolError(msg)
