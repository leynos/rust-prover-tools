"""Read one attempt's shared cgroup evidence from a supplied reader.

The reader is the adapter that touches procfs and the mount table; this module
only decides when to read and how the two reads become one observation. The
membership's own reason travels with the observation, so a membership that
could not be read is reported as missing evidence rather than an absent
hierarchy.
"""

from __future__ import annotations

import dataclasses as dc
import typing as typ

from rust_prover_tools.supervision.models import CgroupObservation

if typ.TYPE_CHECKING:
    from rust_prover_tools.supervision.protocols import CgroupReader

__all__ = ("finish_cgroup", "start_cgroup")


def start_cgroup(reader: CgroupReader) -> CgroupObservation:
    """Read the first cgroup observation before the child starts.

    A membership that could not be read carries its own reason into the
    observation, so the report distinguishes missing evidence from an absent
    hierarchy.
    """
    membership = reader.discover()
    return CgroupObservation(
        path=membership.path,
        start=reader.read(membership.path),
        finish=None,
        unavailable_reason=membership.unavailable_reason,
    )


def finish_cgroup(
    reader: CgroupReader,
    observation: CgroupObservation,
) -> CgroupObservation:
    """Read the finish-time cgroup values for an observation."""
    if observation.path is None:
        return observation
    return dc.replace(observation, finish=reader.read(observation.path))
