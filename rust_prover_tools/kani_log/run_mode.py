"""Report how much of a manifest a Kani log actually covered.

A run's profile is a property of the log and the manifest together: the
manifest is fully covered only when every harness it names appears once and no
block disabled the unwinding assertions that turn a bound into a proof.  The
decision asks this question once, so the two inputs and the tri-state
unwinding check are gathered here rather than in the module that interprets
the outcome itself.
"""

from __future__ import annotations

import typing as typ

from rust_prover_tools.kani_log.extract import unwinding_checks_enabled

if typ.TYPE_CHECKING:
    from rust_prover_tools.kani_log.manifest import Manifest
    from rust_prover_tools.kani_log.model import HarnessBlock
    from rust_prover_tools.kani_log.model import KaniLog as ParsedLog

__all__ = ("FOCUSED", "FULL_PROFILE", "run_mode")

FULL_PROFILE = "full-profile"
FOCUSED = "focused"


def run_mode(log: ParsedLog, manifest: Manifest) -> str:
    """Report ``full-profile`` only when nothing could have been left out.

    Only the selected harnesses count.  ``--harness`` narrows the manifest, and
    a log captured from a wider run still holds blocks for the rest; they were
    not part of the selection, so they neither complete it nor break it.

    Coverage is a set comparison: Kani emits blocks in its own order, so
    comparing positions would report a complete run as focused merely because
    the manifest listed its harnesses differently.
    """
    blocks = _selected_blocks(log, manifest)
    observed = [block.name for block in blocks]
    claimed = manifest.names()
    if len(set(observed)) != len(observed) or set(observed) != set(claimed):
        return FOCUSED
    if not all(_unwinding_enabled(block) for block in blocks):
        return FOCUSED
    return FULL_PROFILE


def _selected_blocks(
    log: ParsedLog,
    manifest: Manifest,
) -> tuple[HarnessBlock, ...]:
    """Return the log's blocks for the harnesses the manifest selects."""
    selected = frozenset(manifest.names())
    return tuple(block for block in log.blocks if block.name in selected)


def _unwinding_enabled(block: HarnessBlock) -> bool:
    """Read the tri-state unwinding check as a yes/no gate."""
    return unwinding_checks_enabled(block) is True
