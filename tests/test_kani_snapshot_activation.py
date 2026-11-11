"""Activation-safety contracts for snapshot installation.

A Hypothesis state machine drives two identities through install, transient
failure, corrupted downloads and repair, checking the pointer and validation
oracle after every step. A stateful sequence cannot prove the filesystem
primitives, so seeded-fault tests then show that a premature pointer write and
a post-setup directory move are both detected.
"""

from __future__ import annotations

import dataclasses as dc
import tempfile
import typing as typ
from pathlib import Path

import pytest
from hypothesis import HealthCheck, settings
from hypothesis import strategies as st
from hypothesis.stateful import (
    RuleBasedStateMachine,
    invariant,
    rule,
    run_state_machine_as_test,
)

from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_snapshot.identity import runtime_for, snapshot_layout
from rust_prover_tools.kani_snapshot.install import check_snapshot, install_snapshot
from rust_prover_tools.kani_snapshot.install_state import load_active, write_active
from rust_prover_tools.kani_snapshot.install_transport import asset_by_role
from tests.kani_snapshot_install_fixtures import (
    active_generation,
    install_hooks,
    install_options,
    make_case,
)

if typ.TYPE_CHECKING:
    from rust_prover_tools.kani_snapshot.install_model import (
        InstallHooks,
        InstallOptions,
    )

_IDENTITIES = ("alpha", "beta")


@dc.dataclass
class _Expectation:
    """What the model believes about one identity's active generation."""

    generation: str | None = None
    is_valid: bool = False
    saved_asset: tuple[Path, bytes] | None = None


class _InstallStateMachine(RuleBasedStateMachine):
    """Two identities exercised through success, failure, corruption and repair.

    The model never reimplements the installer. It asserts around the real
    pipeline: a failed attempt leaves the pointer and the previous generation
    untouched, a valid generation is reused, and a corrupted one is repaired
    without deleting the evidence of the earlier attempt.
    """

    def __init__(self, root: Path) -> None:
        """Create two identity fixtures in a private directory under `root`."""
        super().__init__()
        self.root = Path(tempfile.mkdtemp(dir=root, prefix="machine-"))
        self.cases = {name: make_case(self.root / name) for name in _IDENTITIES}
        self.expectations = {name: _Expectation() for name in _IDENTITIES}

    def pointer(self, identity: str) -> str | None:
        """Return the generation the active pointer names, if any."""
        case = self.cases[identity]
        layout = snapshot_layout(case.pin, cache_home=case.cache_home)
        pointer = load_active(layout)
        return None if pointer is None else pointer.generation

    def options(self, identity: str) -> InstallOptions:
        """Return installation options for one identity."""
        return install_options(self.cases[identity])

    def hooks(self, identity: str) -> InstallHooks:
        """Return the fixture's setup, probe and attestation doubles."""
        return install_hooks(self.cases[identity])

    @invariant()
    def pointer_matches_expectation(self) -> None:
        """Require the active pointer to name only the expected generation."""
        for identity in _IDENTITIES:
            assert self.pointer(identity) == self.expectations[identity].generation, (
                f"active pointer for {identity} drifted from the model"
            )

    @rule(identity=st.sampled_from(_IDENTITIES))
    def install(self, identity: str) -> None:
        """Install, reusing a valid generation and repairing a broken one."""
        case = self.cases[identity]
        expectation = self.expectations[identity]
        previous = expectation.generation
        should_fail = expectation.saved_asset is not None and not expectation.is_valid

        if should_fail:
            with pytest.raises(ProverToolError):
                install_snapshot(self.options(identity), self.hooks(identity))
            assert self.pointer(identity) == previous, (
                "a failed download changed the active pointer"
            )
            return

        install_snapshot(self.options(identity), self.hooks(identity))
        generation = active_generation(case)
        if expectation.is_valid:
            assert generation == previous, "a valid generation was not reused"
        elif previous is not None:
            # Repair must publish a new generation and keep the old tree, so
            # the evidence of the failed attempt survives for diagnosis.
            assert generation != previous, "repair reused a corrupt generation"
            old = snapshot_layout(
                case.pin, cache_home=case.cache_home, generation=previous
            )
            assert old.generation_dir.is_dir(), "repair deleted the old generation"
        expectation.generation = generation
        expectation.is_valid = True
        check_snapshot(self.options(identity), self.hooks(identity))

    @rule(identity=st.sampled_from(_IDENTITIES))
    def refuse_attestation(self, identity: str) -> None:
        """Refuse the attestation without moving the pointer.

        A valid generation short-circuits before staging, so there is nothing
        to refuse; the rule then makes no change rather than asserting a
        failure the installer would not produce.
        """
        if self.expectations[identity].is_valid:
            return
        case = self.cases[identity]
        before = self.pointer(identity)

        def refuse(*_args: object, **_kwargs: object) -> object:
            """Reject every attestation, modelling a transient verifier fault."""
            msg = "attestation refused by the state machine"
            raise ProverToolError(msg)

        with pytest.raises(ProverToolError):
            install_snapshot(
                self.options(identity),
                install_hooks(case, attestation=refuse),
            )
        assert self.pointer(identity) == before, (
            "a refused attestation changed the active pointer"
        )

    @rule(identity=st.sampled_from(_IDENTITIES))
    def corrupt_download(self, identity: str) -> None:
        """Damage a published asset so the next staging attempt fails."""
        if self.expectations[identity].saved_asset is not None:
            return
        case = self.cases[identity]
        path = case.assets_dir / asset_by_role(case.manifest, "launcher").name
        original = path.read_bytes()
        self.expectations[identity].saved_asset = (path, original)
        path.write_bytes(original + b"corrupt")

    @rule(identity=st.sampled_from(_IDENTITIES))
    def restore_download(self, identity: str) -> None:
        """Restore the damaged asset so a later install can succeed."""
        expectation = self.expectations[identity]
        saved = expectation.saved_asset
        if saved is None:
            return
        path, original = saved
        path.write_bytes(original)
        expectation.saved_asset = None

    @rule(identity=st.sampled_from(_IDENTITIES))
    def corrupt_active(self, identity: str) -> None:
        """Damage the active generation so validation must refuse it."""
        expectation = self.expectations[identity]
        if not expectation.is_valid:
            return
        case = self.cases[identity]
        assert expectation.generation is not None, "valid state needs a generation"
        layout = snapshot_layout(
            case.pin,
            cache_home=case.cache_home,
            generation=expectation.generation,
        )
        launcher = runtime_for(layout, case.manifest).launcher
        launcher.write_bytes(launcher.read_bytes() + b"corrupt")
        expectation.is_valid = False
        with pytest.raises(ProverToolError):
            check_snapshot(self.options(identity), self.hooks(identity))

    @rule(identity=st.sampled_from(_IDENTITIES))
    def validate(self, identity: str) -> None:
        """Check validation against the model's own expectation."""
        expectation = self.expectations[identity]
        if expectation.is_valid:
            check_snapshot(self.options(identity), self.hooks(identity))
            return
        with pytest.raises(ProverToolError):
            check_snapshot(self.options(identity), self.hooks(identity))


_MACHINE_SETTINGS = settings(
    max_examples=8,
    stateful_step_count=30,
    deadline=None,
    suppress_health_check=(HealthCheck.too_slow,),
)


def _machine_factory(root: Path) -> type[RuleBasedStateMachine]:
    """Return a zero-argument factory for a machine rooted under `root`.

    Hypothesis documents that the factory may be any callable taking no
    arguments, but annotates the parameter as a class. The cast records that
    upstream annotation mismatch instead of hiding it behind an ignore.
    """

    def build() -> _InstallStateMachine:
        """Create the machine with its own fixture directory."""
        return _InstallStateMachine(root)

    return typ.cast("type[RuleBasedStateMachine]", build)


def test_state_machine_keeps_activation_failure_safe(tmp_path: Path) -> None:
    """Generated operation sequences never leave a partial generation active."""
    run_state_machine_as_test(
        _machine_factory(tmp_path),
        settings=_MACHINE_SETTINGS,
    )


def test_first_install_failure_then_repair_preserves_the_old_generation(
    tmp_path: Path,
) -> None:
    """Both required branches are reachable in one concrete sequence.

    The first install fails before any generation exists; the second works;
    damaging the active tree forces a repair that publishes a new generation
    while the old one stays on disk.
    """
    machine = _InstallStateMachine(tmp_path)
    identity = "alpha"
    machine.refuse_attestation(identity=identity)
    assert machine.pointer(identity) is None, "a failed first install activated"

    machine.install(identity=identity)
    first = machine.pointer(identity)
    assert first is not None, "the repaired install did not activate"

    machine.corrupt_active(identity=identity)
    machine.install(identity=identity)
    second = machine.pointer(identity)
    assert second is not None, "repair did not activate a generation"
    assert second != first, "repair did not republish"
    old = snapshot_layout(
        machine.cases[identity].pin,
        cache_home=machine.cases[identity].cache_home,
        generation=first,
    )
    assert old.generation_dir.is_dir(), "repair destroyed the old generation"


@pytest.mark.timeout(30)
def test_premature_pointer_write_is_detected(tmp_path: Path) -> None:
    """A pointer naming no complete generation cannot pass validation."""
    case = make_case(tmp_path)
    install_snapshot(install_options(case), install_hooks(case))
    layout = snapshot_layout(case.pin, cache_home=case.cache_home)

    write_active(layout, "0" * 12)

    with pytest.raises(ProverToolError):
        check_snapshot(install_options(case), install_hooks(case))


@pytest.mark.timeout(30)
def test_moving_the_active_generation_is_detected(tmp_path: Path) -> None:
    """A post-setup directory move cannot keep validating as active."""
    case = make_case(tmp_path)
    install_snapshot(install_options(case), install_hooks(case))
    layout = snapshot_layout(
        case.pin,
        cache_home=case.cache_home,
        generation=active_generation(case),
    )

    layout.generation_dir.rename(layout.generation_dir.with_name("moved"))

    with pytest.raises(ProverToolError):
        check_snapshot(install_options(case), install_hooks(case))
