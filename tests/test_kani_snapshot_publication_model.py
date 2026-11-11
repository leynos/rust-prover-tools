"""Bounded model of interrupted and resumed publication state.

The install pipeline's correctness after interruption rests on three durable
writes: a publication marker naming the activation sequence it observed, an
ordered activation record, and a completion record the watermark reads. A
single-actor rule-based state machine drives the real writers and readers over
one identity, including writes that land late or were left behind by a killed
worker.

The model asserts the two properties the protocol exists for. A completion
watermark can rise but never regress, however late a completion record lands.
An unfinished publication keeps the cache pending until a later installation
completes above the sequence the marker recorded, after which the marker is no
longer pending and a prune may remove it.
"""

from __future__ import annotations

import json
import tempfile
import typing as typ
from pathlib import Path

from hypothesis import HealthCheck, settings
from hypothesis import strategies as st
from hypothesis.stateful import (
    RuleBasedStateMachine,
    invariant,
    precondition,
    rule,
    run_state_machine_as_test,
)

from rust_prover_tools.kani_snapshot.activation import (
    highest_sequence,
    load_active,
    write_active,
)
from rust_prover_tools.kani_snapshot.completion import completion_watermark
from rust_prover_tools.kani_snapshot.identity import snapshot_layout
from rust_prover_tools.kani_snapshot.install_generation import write_generation_file
from rust_prover_tools.kani_snapshot.install_state import ensure_private_directory
from rust_prover_tools.kani_snapshot.models import SCHEMA_VERSION
from rust_prover_tools.kani_snapshot.publication import (
    Publication,
    marker_base_sequence,
    prune_superseded_markers,
    publication_pending,
    publish_staged,
    render_marker,
)
from tests.kani_snapshot_fixtures import make_pin

#: Width of a completion record's sequence, as `record_completion` writes it.
_COMPLETION_DIGITS = 20


class _PublicationStateMachine(RuleBasedStateMachine):
    """One identity driven through marker, activation and completion writes.

    Interruption is modelled by leaving the durable state an interrupted
    worker would leave: a marker without a completion above it, or a private
    activation document never linked into the order. Resumption is modelled by
    the real repair and prune paths that observe and clear that state.
    """

    def __init__(self, root: Path) -> None:
        """Create one identity layout under `root` and its private directory."""
        super().__init__()
        # Each example gets its own identity directory: Hypothesis reuses the
        # factory's root, and durable state must not leak between examples.
        self.root = Path(tempfile.mkdtemp(dir=root, prefix="publication-"))
        self.layout = snapshot_layout(make_pin(), cache_home=self.root)
        ensure_private_directory(self.layout.identity_dir)
        self.counter = 0
        self.highest = 0
        self.watermark = 0
        self.sequences: list[int] = []
        self.markers: dict[Path, int] = {}
        self.late_records: list[Path] = []
        self.expected_active: str | None = None

    def _next_name(self, prefix: str) -> str:
        """Return a unique name so no marker or generation is ever reused."""
        self.counter += 1
        return f"{prefix}-{self.counter:04d}"

    def _write_marker(self, base: int) -> Path:
        """Write a durable marker naming `base`, as a killed worker would."""
        marker = self.layout.publication_marker(self._next_name("publish"))
        write_generation_file(
            marker,
            render_marker(base),
            "publication marker",
        )
        self.markers[marker] = base
        return marker

    @invariant()
    def watermark_never_regresses(self) -> None:
        """Require the completion watermark to rise but never regress."""
        assert completion_watermark(self.layout) >= self.watermark
        assert highest_sequence(self.layout) >= self.watermark

    @invariant()
    def markers_match_the_model(self) -> None:
        """Every marker on disk is the model's, canonical, and judged rightly."""
        on_disk = set(self.layout.publication_markers())
        assert on_disk == set(self.markers), "publication markers drifted"
        for marker, base in self.markers.items():
            assert marker_base_sequence(marker) == base, "marker bytes lost the base"
        any_pending = any(base >= self.watermark for base in self.markers.values())
        assert publication_pending(self.layout) is any_pending

    @invariant()
    def active_matches_the_model(self) -> None:
        """Activation resolves to the generation the model last recorded."""
        pointer = load_active(self.layout)
        active = None if pointer is None else pointer.generation
        assert active == self.expected_active

    @rule()
    def begin_publication(self) -> None:
        """Leave the marker of a publication interrupted before its payloads."""
        base = highest_sequence(self.layout)
        self._write_marker(base)
        assert publication_pending(self.layout), (
            "an unfinished publication did not keep the cache pending"
        )
        assert completion_watermark(self.layout) == self.watermark

    @rule(index=st.integers(min_value=0, max_value=8))
    def late_marker(self, index: int) -> None:
        """Write a marker late, at a base an earlier publication observed."""
        bases = sorted(set(self.markers.values())) or [0]
        self._write_marker(bases[index % len(bases)])

    @rule()
    def complete_installation(self) -> None:
        """Record an installation above every existing activation."""
        generation = self._next_name("generation")
        write_active(self.layout, generation)
        sequence = highest_sequence(self.layout)
        assert sequence == self.highest + 1, "activation did not take the next slot"
        assert completion_watermark(self.layout) == sequence
        assert not publication_pending(self.layout), (
            "a completion above every marker base left the cache pending"
        )
        assert all(not record.exists() for record in self.late_records)
        self.late_records.clear()
        self.sequences.append(sequence)
        self.highest = sequence
        self.watermark = sequence
        self.expected_active = generation

    @rule(index=st.integers(min_value=0, max_value=8))
    @precondition(lambda self: len(self.sequences) >= 2)
    def late_completion(self, index: int) -> None:
        """Land a completion record beneath the newest, as a late write does."""
        candidates = self.sequences[:-1]
        sequence = candidates[index % len(candidates)]
        record = self.layout.identity_dir / (
            f"completed.{sequence:0{_COMPLETION_DIGITS}d}"
        )
        write_generation_file(
            record,
            f"{sequence}\n".encode("ascii"),
            "completion watermark",
        )
        self.late_records.append(record)
        assert completion_watermark(self.layout) == self.watermark, (
            "a late completion record moved the watermark"
        )

    @rule()
    @precondition(lambda self: bool(self.markers))
    def resume_publication(self) -> None:
        """Republish through a repair that clears every stale marker first."""
        staging = self.root / self._next_name("staging")
        staging.mkdir()
        payload = staging / "payload.bin"
        payload.write_bytes(b"verified payload")
        publication = Publication(
            staging=staging,
            downloads=self.layout.downloads_dir,
            marker=self.layout.publication_marker(self._next_name("publish")),
            stale_markers=tuple(self.markers),
            names=("payload.bin",),
            base_sequence=highest_sequence(self.layout),
        )

        publish_staged(publication)

        assert (self.layout.downloads_dir / "payload.bin").read_bytes() == (
            b"verified payload"
        )
        assert not self.layout.publication_markers(), "a stale marker survived"
        self.markers.clear()
        assert not publication_pending(self.layout)

    @rule()
    def prune_markers(self) -> None:
        """Pruning removes superseded markers and leaves pending ones."""
        prune_superseded_markers(self.layout)
        superseded = [
            marker for marker, base in self.markers.items() if base < self.watermark
        ]
        for marker in superseded:
            del self.markers[marker]
        assert set(self.layout.publication_markers()) == set(self.markers)

    @rule()
    def interrupt_activation(self) -> None:
        """Leave a private activation document a killed worker never linked."""
        self.layout.activations_dir.mkdir(parents=True, exist_ok=True)
        pending = self.layout.activations_dir / (
            f".pending-{self._next_name('worker')}"
        )
        document = json.dumps(
            {
                "schema_version": SCHEMA_VERSION,
                "generation": self._next_name("unlinked"),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        pending.write_text(f"{document}\n", encoding="ascii")

        assert highest_sequence(self.layout) == self.highest
        assert completion_watermark(self.layout) == self.watermark
        pointer = load_active(self.layout)
        active = None if pointer is None else pointer.generation
        assert active == self.expected_active


_MACHINE_SETTINGS = settings(
    max_examples=8,
    stateful_step_count=24,
    deadline=None,
    suppress_health_check=(HealthCheck.too_slow,),
)


def _machine_factory(root: Path) -> type[RuleBasedStateMachine]:
    """Return a zero-argument factory for a machine rooted under `root`.

    Hypothesis documents that the factory may be any callable taking no
    arguments, but annotates the parameter as a class. The cast records that
    upstream annotation mismatch instead of hiding it behind an ignore.
    """

    def build() -> _PublicationStateMachine:
        """Create the machine with its own identity directory."""
        return _PublicationStateMachine(root)

    return typ.cast("type[RuleBasedStateMachine]", build)


def test_publication_state_stays_monotone_and_pending(tmp_path: Path) -> None:
    """Generated interruption and resume orders never break either property."""
    run_state_machine_as_test(
        _machine_factory(tmp_path),
        settings=_MACHINE_SETTINGS,
    )


def test_a_completion_supersedes_an_unfinished_publication(tmp_path: Path) -> None:
    """Concrete witness: a marker stays pending until a later completion."""
    machine = _PublicationStateMachine(tmp_path)
    machine.begin_publication()
    (marker,) = machine.markers
    base = machine.markers[marker]

    assert publication_pending(machine.layout), "the repair was not pending"

    machine.complete_installation()

    assert completion_watermark(machine.layout) > base
    assert not publication_pending(machine.layout), "a later completion left it pending"

    machine.prune_markers()
    assert not marker.exists(), "a superseded marker survived the prune"
