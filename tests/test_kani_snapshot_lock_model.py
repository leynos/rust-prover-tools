"""Two-actor model of the snapshot installation lock.

The process-boundary witnesses in `test_kani_snapshot_concurrency` prove the
cross-process cases. This module models the protocol itself: two named actors
take the real identity lock through separate open file descriptions, so the
kernel excludes them exactly as it excludes separate processes, and a
Hypothesis state machine interleaves acquisition, release and installation
against one identity. The model never tells the installer which actor is
calling; it asserts that a refusal happens exactly when the peer is inside the
pipeline and that the pointer names only the last completed install.
"""

from __future__ import annotations

import contextlib
import dataclasses as dc
import functools
import shutil
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
from rust_prover_tools.kani_snapshot.identity import snapshot_layout
from rust_prover_tools.kani_snapshot.install import (
    check_active_snapshot,
    install_snapshot,
)
from rust_prover_tools.kani_snapshot.install_state import identity_lock, load_active
from tests.kani_snapshot_install_fixtures import (
    active_generation,
    install_hooks,
    install_options,
    make_case,
)

if typ.TYPE_CHECKING:
    from rust_prover_tools.kani_snapshot.install_model import InstallOptions

_ACTORS = ("first", "second")

_SETTINGS = settings(
    max_examples=8,
    stateful_step_count=25,
    deadline=None,
    suppress_health_check=(HealthCheck.too_slow,),
)


class _TwoActorStateMachine(RuleBasedStateMachine):
    """Two actors contending for one identity's real pipeline lock."""

    def __init__(self, root: Path) -> None:
        """Create one identity fixture shared by both actors."""
        super().__init__()
        self.root = Path(tempfile.mkdtemp(dir=root, prefix="actors-"))
        self.case = make_case(self.root)
        self.held: dict[str, contextlib.ExitStack] = {}
        self.generation: str | None = None

    def teardown(self) -> None:
        """Release any lock an interrupted rule left held."""
        for stack in self.held.values():
            stack.close()
        self.held.clear()
        shutil.rmtree(self.root, ignore_errors=True)
        super().teardown()

    def pointer(self) -> str | None:
        """Return the generation the active pointer names, if any."""
        layout = snapshot_layout(self.case.pin, cache_home=self.case.cache_home)
        pointer = load_active(layout)
        return None if pointer is None else pointer.generation

    def options(self, *, lock_timeout_seconds: float = 10) -> InstallOptions:
        """Return options with the wait the model intends to exercise."""
        return dc.replace(
            install_options(self.case),
            lock_timeout_seconds=lock_timeout_seconds,
        )

    @invariant()
    def pointer_matches_the_completed_install(self) -> None:
        """Require the pointer to name only the last completed install."""
        assert self.pointer() == self.generation

    @rule(actor=st.sampled_from(_ACTORS))
    def acquire(self, actor: str) -> None:
        """Let one actor take the pipeline lock and hold it across steps.

        The lock is exclusive, so a second acquisition is inapplicable and the
        refusal path is exercised through `install` instead.
        """
        if self.held:
            return
        stack = contextlib.ExitStack()
        stack.enter_context(identity_lock(self.options(), timeout=0.2))
        self.held[actor] = stack

    @rule(actor=st.sampled_from(_ACTORS))
    def release(self, actor: str) -> None:
        """Release the lock if this actor is the one holding it."""
        stack = self.held.pop(actor, None)
        if stack is not None:
            stack.close()

    @rule(actor=st.sampled_from(_ACTORS))
    def install(self, actor: str) -> None:
        """Install as one actor, refusing only when the peer holds the lock.

        An actor that already holds the lock would contend with its own open
        file description, which the pipeline never does, so that case makes no
        change rather than asserting a refusal.
        """
        if actor in self.held:
            return
        options = self.options(lock_timeout_seconds=0.2)
        if self.held:
            with pytest.raises(ProverToolError, match="timed out"):
                install_snapshot(options, install_hooks(self.case))
            return
        install_snapshot(options, install_hooks(self.case))
        self.generation = active_generation(self.case)
        check_active_snapshot(self.options(), install_hooks(self.case))


def _machine_factory(root: Path) -> type[RuleBasedStateMachine]:
    """Return a zero-argument factory for the two-actor machine."""
    build = functools.partial(_TwoActorStateMachine, root)
    return typ.cast("type[RuleBasedStateMachine]", build)


def test_two_actor_model_keeps_one_valid_generation(tmp_path: Path) -> None:
    """Interleaved acquisition, refusal and install preserve the pointer."""
    run_state_machine_as_test(_machine_factory(tmp_path), settings=_SETTINGS)
