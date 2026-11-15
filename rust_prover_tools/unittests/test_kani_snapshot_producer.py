"""Tests for the pinned upstream Kani snapshot build recipe.

The recipe fixes the upstream source identity, toolchain, build prerequisites,
launcher commands and licence evidence that later producer milestones consume.
These tests keep it consistent with the checked-in upstream evidence and with
the canonical serialisation whose digest the producer manifest records.
"""

from __future__ import annotations

import dataclasses as dc
import json
import pathlib
import typing as typ

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_snapshot.attestation import (
    attestation_command,
    policy_for_pin,
)
from rust_prover_tools.kani_snapshot.producer.commands import (
    bundle_asset_name,
    bundle_command,
    launcher_asset_name,
    launcher_install_command,
    launcher_package_command,
    setup_command,
    toolchain_install_command,
)
from rust_prover_tools.kani_snapshot.producer.compiler_floor import (
    COMPILER_FLOOR,
    parse_rustc_version,
)
from rust_prover_tools.kani_snapshot.producer.recipe import (
    PINNED_UPSTREAM_COMMIT,
    PINNED_UPSTREAM_TAG,
    pinned_recipe,
    recipe_digest,
    recipe_to_json,
)
from rust_prover_tools.kani_snapshot.producer.recipe_model import (
    LauncherRecipe,
    LicenceRecord,
    Recipe,
)
from rust_prover_tools.kani_snapshot.producer.recipe_parse import parse_recipe
from rust_prover_tools.kani_snapshot.producer.recipe_rules import validate_recipe
from rust_prover_tools.kani_snapshot.producer.resolution import (
    REQUIRED_UPSTREAM_CHECKS,
    CandidateCommit,
    CheckRun,
    candidate_from_api,
    resolve_manual_commit,
    select_newest_candidate,
)
from rust_prover_tools.kani_snapshot.producer.upstream_metadata import (
    missing_licence_evidence,
    parse_crate_version,
    parse_dependency_versions,
    parse_licence_inventory,
    parse_submodule_paths,
    parse_toolchain_channel,
)
from tests.kani_snapshot_fixtures import make_pin

if typ.TYPE_CHECKING:
    from syrupy.assertion import SnapshotAssertion

FIXTURES: typ.Final[pathlib.Path] = (
    pathlib.Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "kani_recipe"
)


def _fixture(name: str) -> bytes:
    """Return the exact bytes of one checked-in upstream evidence file."""
    return (FIXTURES / name).read_bytes()


def _fixture_json(name: str) -> object:
    """Return one checked-in evidence file parsed as JSON."""
    return json.loads(_fixture(name))


def _mutated(recipe: Recipe, **upstream_changes: object) -> Recipe:
    """Return the recipe with selected upstream fields replaced."""
    return dc.replace(
        recipe,
        upstream=dc.replace(recipe.upstream, **upstream_changes),
    )


def _checks(*names: str) -> tuple[CheckRun, ...]:
    """Return successful completed runs, each with a distinct identity."""
    return tuple(
        CheckRun(
            name=name,
            status="completed",
            conclusion="success",
            run_id=index + 1,
            started_at=f"2026-09-16T00:00:{index:02d}Z",
        )
        for index, name in enumerate(names)
    )


def test_pinned_recipe_matches_the_upstream_evidence() -> None:
    """Every pinned identity agrees with the checked-in upstream files."""
    recipe = pinned_recipe()
    release = typ.cast("dict[str, object]", _fixture_json("upstream-release.json"))
    assert recipe.upstream.commit == PINNED_UPSTREAM_COMMIT
    assert recipe.upstream.tag == PINNED_UPSTREAM_TAG
    assert recipe.upstream.commit == release["commit"]
    assert recipe.upstream.tag == release["tag"]
    assert recipe.upstream.published_at == release["published_at"]
    assert recipe.upstream.repository == "model-checking/kani"
    assert (
        parse_toolchain_channel(_fixture("rust-toolchain.toml"))
        == recipe.upstream.toolchain_channel
    ), "toolchain channel drifted"
    assert (
        parse_crate_version(_fixture("Cargo.toml")) == recipe.upstream.crate_version
    ), "crate version drifted"
    assert recipe.target == "x86_64-unknown-linux-gnu"
    assert recipe.compiler_floor == (1, 94)


def test_pinned_recipe_records_the_upstream_dependency_versions() -> None:
    """The prerequisite records name the versions upstream builds with."""
    recipe = pinned_recipe()
    dependencies = parse_dependency_versions(_fixture("kani-dependencies"))
    prerequisites = {record.name: record for record in recipe.prerequisites}
    assert prerequisites["cbmc"].version == dependencies["CBMC_VERSION"]
    assert prerequisites["kissat"].version == dependencies["KISSAT_VERSION"]
    assert "ubuntu-22.04" in prerequisites["cbmc"].source, "build host drifted"
    assert prerequisites["cbmc"].sha256 == (
        "de9fa1cc4cf51c493e708a1c7608b192ad4956717b684d9c4501e22fd22d88a0"
    )
    assert prerequisites["kissat"].sha256 == (
        "4b41edf12ffa5f8e8b1986e5ad3e0bedb4d34b0ed3ecc7c13362bc7ba0aba66b"
    )


def test_pinned_recipe_records_every_source_submodule() -> None:
    """Recorded gitlinks match the upstream tree and its `.gitmodules`."""
    recipe = pinned_recipe()
    declared = parse_submodule_paths(_fixture("gitmodules"))
    recorded = {record.path: record.commit for record in recipe.submodules}
    assert tuple(sorted(recorded)) == tuple(sorted(declared))
    assert recorded == _fixture_json("upstream-gitlinks.json")


def test_pinned_recipe_has_complete_licence_evidence() -> None:
    """Upstream's licence notes evidence every notice-listed component."""
    recipe = pinned_recipe()
    notes = _fixture("license-notes.txt")
    assert missing_licence_evidence(notes, recipe.licences) == ()
    components = {record.component for record in recipe.licences}
    assert {"kani", "rustc", "cbmc", "kissat"} <= components
    assert "kissat" not in parse_licence_inventory(notes), (
        "Kissat is redistributed but absent from upstream's notes, so it must "
        "carry its own licence source"
    )


def test_pinned_recipe_is_valid() -> None:
    """The pinned recipe satisfies its own invariants."""
    validate_recipe(pinned_recipe())


def test_validation_rejects_a_short_upstream_commit() -> None:
    """A recipe may not name a mutable or abbreviated commit."""
    recipe = _mutated(pinned_recipe(), commit="4e31125")
    with pytest.raises(ProverToolError, match="full lowercase 40-hex"):
        validate_recipe(recipe)


def test_validation_rejects_a_crate_version_mismatch() -> None:
    """The upstream version is part of the identity, not free text."""
    recipe = _mutated(pinned_recipe(), crate_version="")
    with pytest.raises(ProverToolError, match="crate_version"):
        validate_recipe(recipe)


def test_validation_rejects_a_tag_that_disagrees_with_the_version() -> None:
    """The release tag is derived from the crate version."""
    recipe = _mutated(pinned_recipe(), tag="kani-0.67.0")
    with pytest.raises(ProverToolError, match="tag"):
        validate_recipe(recipe)


def test_validation_rejects_an_unknown_toolchain_channel() -> None:
    """Only a dated nightly channel is a reproducible toolchain identity."""
    recipe = _mutated(pinned_recipe(), toolchain_channel="nightly")
    with pytest.raises(ProverToolError, match="toolchain_channel"):
        validate_recipe(recipe)


def test_validation_rejects_a_floor_below_the_supported_minimum() -> None:
    """A recipe cannot lower the compiler floor it claims to demonstrate."""
    recipe = dc.replace(pinned_recipe(), compiler_floor=(1, 93))
    with pytest.raises(ProverToolError, match="compiler floor"):
        validate_recipe(recipe)


def test_validation_rejects_a_floor_above_the_pinned_compiler() -> None:
    """A recipe cannot promise a compiler it does not build with."""
    recipe = dc.replace(pinned_recipe(), compiler_floor=(1, 101))
    with pytest.raises(ProverToolError, match="newer than the pinned compiler"):
        validate_recipe(recipe)


def test_pinned_builder_supports_the_claimed_runtime_floor() -> None:
    """The builder image is the oldest glibc the published bundle may claim."""
    recipe = pinned_recipe()
    assert recipe.builder.image == "ubuntu-22.04"
    assert recipe.builder.glibc == recipe.runtime.glibc_baseline


def test_validation_rejects_a_runtime_floor_newer_than_the_builder() -> None:
    """A bundle cannot run on a glibc older than the one it was linked against."""
    recipe = dc.replace(
        pinned_recipe(),
        builder=dc.replace(pinned_recipe().builder, glibc="2.31"),
    )
    with pytest.raises(ProverToolError, match="newer than the builder glibc"):
        validate_recipe(recipe)


def test_validation_rejects_a_malformed_builder_image() -> None:
    """The builder is a plain image label, never a command or a URL."""
    recipe = dc.replace(
        pinned_recipe(),
        builder=dc.replace(pinned_recipe().builder, image="ubuntu-22.04; id"),
    )
    with pytest.raises(ProverToolError, match="builder image"):
        validate_recipe(recipe)


def test_validation_requires_cbmc_and_kissat() -> None:
    """The bundle cannot be built without both redistributed tools."""
    recipe = pinned_recipe()
    kept = tuple(record for record in recipe.prerequisites if record.name != "kissat")
    with pytest.raises(ProverToolError, match="kissat"):
        validate_recipe(dc.replace(recipe, prerequisites=kept))


def test_validation_requires_the_matching_launcher_binaries() -> None:
    """A launcher archive must provide both upstream entry points."""
    recipe = dc.replace(
        pinned_recipe(),
        launcher=LauncherRecipe(
            package="kani-verifier",
            binaries=("kani",),
            archive_template="kani-{version}-launcher-{target}.tar.gz",
        ),
    )
    with pytest.raises(ProverToolError, match="cargo-kani"):
        validate_recipe(recipe)


def test_validation_requires_licence_evidence_for_kissat() -> None:
    """A redistributed component may not fall out of the notices."""
    recipe = pinned_recipe()
    kept = tuple(record for record in recipe.licences if record.component != "kissat")
    with pytest.raises(ProverToolError, match="kissat"):
        validate_recipe(dc.replace(recipe, licences=kept))


def test_validation_rejects_an_unknown_attestation_verifier() -> None:
    """The attestation boundary is the documented GitHub CLI."""
    recipe = dc.replace(
        pinned_recipe(),
        attestation=dc.replace(pinned_recipe().attestation, verifier="cosign"),
    )
    with pytest.raises(ProverToolError, match="attestation verifier"):
        validate_recipe(recipe)


def test_attestation_command_prefix_matches_the_recipe() -> None:
    """The frozen recipe prefix is the vector the verifier actually runs."""
    recipe = pinned_recipe()
    argv = attestation_command(
        policy_for_pin(make_pin()),
        pathlib.Path("manifest.json"),
        pathlib.Path("manifest.sigstore.jsonl"),
    )
    assert argv[: len(recipe.attestation.command_prefix)] == (
        recipe.attestation.command_prefix
    )


def test_canonical_json_is_sorted_and_newline_terminated() -> None:
    """The serialised recipe is deterministic and byte-stable."""
    first = recipe_to_json(pinned_recipe())
    second = recipe_to_json(pinned_recipe())
    assert first == second
    assert first.startswith(b'{\n  "attestation"')
    assert first.endswith(b"\n")


def test_canonical_json_round_trips() -> None:
    """The strict parser accepts exactly what the renderer produces."""
    recipe = pinned_recipe()
    assert parse_recipe(recipe_to_json(recipe)) == recipe


def test_canonical_json_matches_the_reviewed_snapshot(
    snapshot: SnapshotAssertion,
) -> None:
    """A pinned-value change appears as a reviewed snapshot diff.

    The digest proves the bytes changed; only the snapshot shows what changed:
    which upstream identity, prerequisite digest or licence evidence moved.
    """
    assert recipe_to_json(pinned_recipe()).decode("ascii") == snapshot


@settings(max_examples=50, deadline=None)
@given(
    commit=st.from_regex(r"[0-9a-f]{40}", fullmatch=True),
    version=st.from_regex(r"\d+\.\d+\.\d+", fullmatch=True),
    channel=st.from_regex(r"nightly-\d{4}-\d{2}-\d{2}", fullmatch=True),
)
def test_serialised_recipe_round_trips_for_generated_identities(
    commit: str,
    version: str,
    channel: str,
) -> None:
    """The renderer and parser agree for any well-formed upstream identity."""
    recipe = _mutated(
        pinned_recipe(),
        commit=commit,
        crate_version=version,
        tag=f"kani-{version}",
        toolchain_channel=channel,
    )
    reparsed = parse_recipe(recipe_to_json(recipe))
    assert reparsed == recipe
    assert recipe_digest(reparsed) == recipe_digest(recipe)


def test_digest_changes_when_any_identity_field_changes() -> None:
    """A changed input cannot keep the recorded recipe digest."""
    recipe = pinned_recipe()
    digest = recipe_digest(recipe)
    mutations = (
        _mutated(recipe, commit="0" * 39 + "1"),
        _mutated(recipe, crate_version="0.68.1", tag="kani-0.68.1"),
        _mutated(recipe, toolchain_channel="nightly-2026-09-24"),
        dc.replace(recipe, compiler_floor=(1, 95)),
        dc.replace(recipe, target="aarch64-unknown-linux-gnu"),
    )
    for mutated in mutations:
        assert recipe_digest(mutated) != digest


def _document() -> dict[str, typ.Any]:
    """Return the pinned recipe as a parsed JSON document."""
    return typ.cast("dict[str, typ.Any]", json.loads(recipe_to_json(pinned_recipe())))


def _encode(document: object) -> bytes:
    """Encode a document as JSON bytes."""
    return json.dumps(document).encode()


def test_parse_rejects_an_unknown_key() -> None:
    """An unrecognised recipe key fails closed."""
    document = _document()
    document["surprise"] = 1
    with pytest.raises(ProverToolError, match="unknown keys"):
        parse_recipe(_encode(document))


def test_parse_rejects_a_missing_key() -> None:
    """A missing required group fails closed."""
    document = _document()
    del document["runtime"]
    with pytest.raises(ProverToolError, match="missing keys"):
        parse_recipe(_encode(document))


def test_parse_rejects_a_wrong_type() -> None:
    """Schema version one is an integer, not a boolean or a string."""
    document = _document()
    document["schema_version"] = True
    with pytest.raises(ProverToolError, match="integer"):
        parse_recipe(_encode(document))


@pytest.mark.parametrize("version", [2, 0, -1])
def test_parse_rejects_an_unsupported_schema_version(version: int) -> None:
    """Only schema version one is understood."""
    document = _document()
    document["schema_version"] = version
    with pytest.raises(ProverToolError, match="schema_version"):
        parse_recipe(_encode(document))


def test_parse_rejects_a_duplicate_key() -> None:
    """A repeated JSON key is refused rather than last-one-wins."""
    data = recipe_to_json(pinned_recipe()).replace(
        b'"schema_version": 1',
        b'"schema_version": 1, "schema_version": 1',
        1,
    )
    with pytest.raises(ProverToolError, match="duplicate key"):
        parse_recipe(data)


def test_bundle_and_launcher_names_follow_the_upstream_grammar() -> None:
    """Asset names are derived, never free text."""
    assert bundle_asset_name("0.68.0", "x86_64-unknown-linux-gnu") == (
        "kani-0.68.0-x86_64-unknown-linux-gnu.tar.gz"
    )
    assert launcher_asset_name("0.68.0", "x86_64-unknown-linux-gnu") == (
        "kani-0.68.0-launcher-x86_64-unknown-linux-gnu.tar.gz"
    )


def test_build_commands_are_argument_vectors() -> None:
    """No command is assembled through a shell string."""
    assert bundle_command("0.68.0") == ("cargo", "bundle", "--", "0.68.0")
    assert launcher_package_command("kani-verifier") == (
        "cargo",
        "package",
        "--package",
        "kani-verifier",
    )
    assert launcher_install_command(
        pathlib.Path("kani-verifier-0.68.0"),
        pathlib.Path("prefix"),
    ) == (
        "cargo",
        "install",
        "--locked",
        "--path",
        "kani-verifier-0.68.0",
        "--root",
        "prefix",
    )
    assert setup_command(
        pathlib.Path("kani-0.68.0.tar.gz"),
        pathlib.Path("toolchain"),
    ) == (
        "cargo",
        "kani",
        "setup",
        "--use-local-bundle",
        "kani-0.68.0.tar.gz",
        "--use-local-toolchain",
        "toolchain",
    )
    assert toolchain_install_command("nightly-2026-08-21")[:3] == (
        "rustup",
        "toolchain",
        "install",
    )


@pytest.mark.parametrize(
    "version",
    ["", "0.68.0 ", "0.68.0; rm -rf /", "$(id)", "../0.68.0", "latest"],
)
def test_bundle_command_rejects_a_non_version_selector(version: str) -> None:
    """Only a plain semantic version may reach the bundle command."""
    with pytest.raises(ProverToolError, match="version"):
        bundle_command(version)


def test_metadata_parsers_refuse_malformed_evidence() -> None:
    """A malformed upstream file is an error, not a default value."""
    with pytest.raises(ProverToolError, match="dated nightly"):
        parse_toolchain_channel(b'[toolchain]\nchannel = "nightly"\n')
    with pytest.raises(ProverToolError, match=r"\[package\]"):
        parse_crate_version(b"[workspace]\nmembers = []\n")
    with pytest.raises(ProverToolError, match="NAME"):
        parse_dependency_versions(b"CBMC_VERSION=6.11.0\n")
    with pytest.raises(ProverToolError, match="no path"):
        parse_submodule_paths(
            b'[submodule "charon"]\n\turl = https://example.invalid\n'
        )


def test_missing_licence_evidence_flags_a_notice_lie() -> None:
    """A component cannot claim notice coverage the notes do not give."""
    notes = _fixture("license-notes.txt")
    claimed = LicenceRecord(
        component="z3",
        licence="MIT",
        source="https://example.invalid/z3",
        evidence="licence-notes",
    )
    assert missing_licence_evidence(notes, (claimed,)) == ("z3",)


@pytest.mark.parametrize(
    "value",
    [
        "4e31125",
        "4E31125FDB4F01FAD95150415CBAFE46E0587D66",
        "main",
        "--upload-pack=evil",
        "https://github.com/model-checking/kani",
        "",
    ],
)
def test_manual_commit_rejects_anything_but_a_full_sha(value: str) -> None:
    """A manual dispatch input is an identity, never a ref or a command."""
    with pytest.raises(ProverToolError, match="full lowercase 40-hex"):
        resolve_manual_commit(value)


def test_manual_commit_accepts_a_full_sha() -> None:
    """The pinned commit is the manual-input form."""
    assert resolve_manual_commit(PINNED_UPSTREAM_COMMIT) == PINNED_UPSTREAM_COMMIT


def _candidate(commit: str, published_at: str, *checks: CheckRun) -> CandidateCommit:
    """Return one candidate commit with its observed checks."""
    return CandidateCommit(
        commit=commit,
        tag=f"kani-{published_at}",
        published_at=published_at,
        checks=tuple(checks),
    )


_ALL_REQUIRED = _checks(*REQUIRED_UPSTREAM_CHECKS)
_OTHER_REQUIRED = tuple(
    check for check in _ALL_REQUIRED if check.name != REQUIRED_UPSTREAM_CHECKS[0]
)
_NEWEST = "1" * 40
_OLDER = "2" * 40


def test_selection_takes_the_newest_candidate_with_every_check() -> None:
    """A missing required check disqualifies an otherwise newer release."""
    missing = _OTHER_REQUIRED
    assert (
        select_newest_candidate([
            _candidate(_NEWEST, "2026-10-01T00:00:00Z", *missing),
            _candidate(_OLDER, "2026-09-01T00:00:00Z", *_ALL_REQUIRED),
        ])
        == _OLDER
    )


def test_selection_prefers_the_newest_qualified_candidate() -> None:
    """Publication order decides between two qualified releases."""
    assert (
        select_newest_candidate([
            _candidate(_OLDER, "2026-09-01T00:00:00Z", *_ALL_REQUIRED),
            _candidate(_NEWEST, "2026-10-01T00:00:00Z", *_ALL_REQUIRED),
        ])
        == _NEWEST
    )


def test_selection_uses_the_newest_run_of_a_check() -> None:
    """A rerun that succeeded clears an earlier failure of the same check."""
    fixed = CheckRun(
        name=REQUIRED_UPSTREAM_CHECKS[0],
        status="completed",
        conclusion="success",
        run_id=99,
        started_at="2026-09-17T00:00:00Z",
    )
    failure = CheckRun(
        name=REQUIRED_UPSTREAM_CHECKS[0],
        status="completed",
        conclusion="failure",
        run_id=1,
        started_at="2026-09-16T00:00:00Z",
    )
    rest = _OTHER_REQUIRED
    assert (
        select_newest_candidate([
            _candidate(_NEWEST, "2026-10-01T00:00:00Z", failure, fixed, *rest)
        ])
        == _NEWEST
    )


def test_selection_skips_a_failed_latest_check() -> None:
    """A newer failure of a required check disqualifies the candidate."""
    broken = CheckRun(
        name=REQUIRED_UPSTREAM_CHECKS[0],
        status="completed",
        conclusion="failure",
        run_id=99,
        started_at="2026-09-17T00:00:00Z",
    )
    rest = _OTHER_REQUIRED
    assert (
        select_newest_candidate([
            _candidate(_NEWEST, "2026-10-01T00:00:00Z", broken, *rest),
            _candidate(_OLDER, "2026-09-01T00:00:00Z", *_ALL_REQUIRED),
        ])
        == _OLDER
    )


def test_selection_skips_an_incomplete_check() -> None:
    """A queued check is not evidence of success."""
    pending = CheckRun(
        name=REQUIRED_UPSTREAM_CHECKS[0],
        status="in_progress",
        conclusion=None,
        run_id=99,
        started_at="2026-09-17T00:00:00Z",
    )
    rest = _OTHER_REQUIRED
    assert (
        select_newest_candidate([
            _candidate(_NEWEST, "2026-10-01T00:00:00Z", pending, *rest),
            _candidate(_OLDER, "2026-09-01T00:00:00Z", *_ALL_REQUIRED),
        ])
        == _OLDER
    )


def test_selection_fails_closed_on_an_unordered_duplicate() -> None:
    """Two runs of one check that cannot be ordered are ambiguous."""
    commit = _NEWEST
    failed = CheckRun(
        name=REQUIRED_UPSTREAM_CHECKS[0],
        status="completed",
        conclusion="failure",
        run_id=1,
        started_at="2026-09-16T00:00:00Z",
    )
    success = dc.replace(
        _ALL_REQUIRED[0],
        started_at=failed.started_at,
        run_id=failed.run_id,
    )
    with pytest.raises(ProverToolError, match="ambiguous"):
        select_newest_candidate([
            _candidate(
                commit,
                "2026-10-01T00:00:00Z",
                failed,
                success,
                *_OTHER_REQUIRED,
            )
        ])


def test_selection_fails_closed_without_a_qualified_candidate() -> None:
    """An empty or wholly unqualified set never yields a commit."""
    with pytest.raises(ProverToolError, match="no qualified"):
        select_newest_candidate([])
    with pytest.raises(ProverToolError, match="no qualified"):
        select_newest_candidate([_candidate(_NEWEST, "2026-10-01T00:00:00Z")])


def test_selection_rejects_a_duplicated_candidate_commit() -> None:
    """The same commit twice is ambiguous evidence, not a tie."""
    with pytest.raises(ProverToolError, match="repeat"):
        select_newest_candidate([
            _candidate(_NEWEST, "2026-10-01T00:00:00Z", *_ALL_REQUIRED),
            _candidate(_NEWEST, "2026-09-01T00:00:00Z", *_ALL_REQUIRED),
        ])


def test_required_checks_are_the_linux_x86_64_jobs() -> None:
    """The required set covers both tested glibc baselines and nothing else."""
    assert REQUIRED_UPSTREAM_CHECKS == (
        "BuildBundle-Linux-x86_64",
        "TestBundle (ubuntu-22.04)",
        "TestBundle (ubuntu-24.04)",
        "TestLocalToolchain (ubuntu-22.04)",
        "TestLocalToolchain (ubuntu-24.04)",
        "regression (ubuntu-22.04)",
        "regression (ubuntu-24.04)",
    )


def test_pinned_release_is_selected_from_upstream_evidence() -> None:
    """The recorded release qualifies from the real check-run evidence."""
    release = typ.cast("dict[str, object]", _fixture_json("upstream-release.json"))
    checks = typ.cast("list[object]", _fixture_json("upstream-check-runs.json"))
    candidate = candidate_from_api(release, checks)
    newer = _candidate(_NEWEST, "2026-10-01T00:00:00Z")
    assert select_newest_candidate([newer, candidate]) == PINNED_UPSTREAM_COMMIT
    recipe = pinned_recipe()
    assert recipe.upstream.commit == candidate.commit
    assert recipe.upstream.tag == candidate.tag
    assert recipe.upstream.published_at == candidate.published_at


def test_candidate_parser_refuses_a_draft_release() -> None:
    """A draft release is not a published identity."""
    release = typ.cast("dict[str, object]", _fixture_json("upstream-release.json"))
    draft = {**release, "draft": True}
    checks = typ.cast("list[object]", _fixture_json("upstream-check-runs.json"))
    with pytest.raises(ProverToolError, match="draft"):
        candidate_from_api(draft, checks)


def test_candidate_parser_refuses_a_malformed_check_run() -> None:
    """A check run without a status is external data the parser refuses."""
    release = typ.cast("dict[str, object]", _fixture_json("upstream-release.json"))
    with pytest.raises(ProverToolError, match="status"):
        candidate_from_api(release, [{"id": 1, "name": "x", "conclusion": None}])


def test_pinned_toolchain_reaches_the_compiler_floor() -> None:
    """The pinned toolchain compiles above the 1.94 floor."""
    recipe = pinned_recipe()
    assert parse_rustc_version(recipe.upstream.rustc_version) >= COMPILER_FLOOR
