"""Value-level checks shared by the versioned schema readers.

These helpers decide what a field's value may be, not which fields exist. The
field sets live in ``schema_fields`` and the document readers in ``schema``, so
a producer and a reader can compare one definition and each refusal names the
field it rejected.
"""

from __future__ import annotations

import math
import typing as typ

if typ.TYPE_CHECKING:
    import collections.abc as cabc

__all__ = (
    "EvidenceFormatError",
    "decode_measurement",
    "require_bool",
    "require_enum",
    "require_exact",
    "require_int",
    "require_optional",
    "require_optional_int",
    "require_seconds",
    "require_str",
    "validate_accounting",
    "validate_cgroup",
)


class EvidenceFormatError(ValueError):
    """A record or report that does not match the documented schema."""


def require_exact(
    document: cabc.Mapping[str, object],
    fields: frozenset[str],
    *,
    subject: str,
) -> None:
    """Require exactly `fields`, naming the missing and unknown keys."""
    missing = sorted(fields - set(document))
    unknown = sorted(set(document) - fields)
    if missing or unknown:
        msg = (
            f"{subject} does not match the schema: missing={missing}, unknown={unknown}"
        )
        raise EvidenceFormatError(msg)


def require_optional(
    document: cabc.Mapping[str, object],
    permitted: frozenset[str],
    *,
    required: frozenset[str],
) -> None:
    """Require `required`, permit `permitted`, and reject anything else."""
    missing = sorted(required - set(document))
    unknown = sorted(set(document) - permitted)
    if missing or unknown:
        msg = f"the record has missing={missing} and unknown={unknown}"
        raise EvidenceFormatError(msg)


def require_enum(
    value: object,
    allowed: type[typ.Any],
    *,
    subject: str,
) -> None:
    """Require one documented enum value, not merely a string."""
    names = {member.value for member in allowed}
    if not isinstance(value, str) or value not in names:
        msg = f"{subject} is not a documented value: {value!r}"
        raise EvidenceFormatError(msg)


def require_bool(value: object, *, subject: str) -> None:
    """Require a Boolean, not an integer that happens to be 0 or 1."""
    if not isinstance(value, bool):
        msg = f"{subject} is not a Boolean"
        raise EvidenceFormatError(msg)


def require_int(
    value: object,
    *,
    subject: str,
    minimum: int | None = None,
) -> None:
    """Require an exact integer, refusing a Boolean or a float."""
    if not isinstance(value, int) or isinstance(value, bool):
        msg = f"{subject} is not an integer"
        raise EvidenceFormatError(msg)
    if minimum is not None and value < minimum:
        msg = f"{subject} is below {minimum}"
        raise EvidenceFormatError(msg)


def require_optional_int(value: object, *, subject: str) -> None:
    """Require an integer or `None`."""
    if value is not None:
        require_int(value, subject=subject)


def require_str(
    value: object,
    *,
    subject: str,
    optional: bool = False,
) -> None:
    """Require a string, or `None` when the field is optional."""
    if value is None and optional:
        return
    if not isinstance(value, str):
        msg = f"{subject} is not a string"
        raise EvidenceFormatError(msg)
    if not optional and not value:
        msg = f"{subject} is an empty string"
        raise EvidenceFormatError(msg)


def require_seconds(value: object, *, subject: str) -> None:
    """Require a finite non-negative number of seconds."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        msg = f"{subject} is not a number"
        raise EvidenceFormatError(msg)
    if not math.isfinite(value):
        msg = f"{subject} is not finite"
        raise EvidenceFormatError(msg)
    if value < 0:
        msg = f"{subject} is negative"
        raise EvidenceFormatError(msg)


def measurement_keys(document: object, *, subject: str) -> None:
    """Require a measurement to be exactly one available or absent form."""
    if not isinstance(document, dict):
        msg = f"{subject} is not a measurement object"
        raise EvidenceFormatError(msg)
    keys = set(document)
    if keys == {"value"}:
        value = document["value"]
        if not isinstance(value, int) or isinstance(value, bool):
            msg = f"{subject}.value is not an integer"
            raise EvidenceFormatError(msg)
        if value < 0:
            msg = f"{subject}.value is negative"
            raise EvidenceFormatError(msg)
        return
    if keys == {"unavailable_reason"}:
        if not isinstance(document["unavailable_reason"], str):
            msg = f"{subject}.unavailable_reason is not a string"
            raise EvidenceFormatError(msg)
        return
    msg = f"{subject} is neither available nor unavailable: {sorted(keys)}"
    raise EvidenceFormatError(msg)


def validate_accounting(accounting: object, *, subject: str = "accounting") -> None:
    """Require the accounting object to carry the three drift lists."""
    if not isinstance(accounting, dict):
        msg = f"{subject} is not an object"
        raise EvidenceFormatError(msg)
    require_exact(
        accounting,
        frozenset({"missing", "duplicate", "unexpected"}),
        subject=subject,
    )
    for field in ("missing", "duplicate", "unexpected"):
        entries = accounting[field]
        if not isinstance(entries, list):
            msg = f"{subject} {field} is not an array"
            raise EvidenceFormatError(msg)
        for index, entry in enumerate(entries):
            if not isinstance(entry, str) or not entry:
                msg = f"{subject} {field}[{index}] is not a non-empty identity string"
                raise EvidenceFormatError(msg)


def decode_measurement(document: object, *, subject: str = "measurement") -> None:
    """Validate one measurement object.

    Parameters
    ----------
    document : object
        Parsed JSON value.
    subject : str
        Name used in the refusal.

    Raises
    ------
    EvidenceFormatError
        Raised when the value is neither ``{"value": N}`` nor
        ``{"unavailable_reason": "..."}``.
    """
    measurement_keys(document, subject=subject)


def validate_cgroup(document: object, *, subject: str) -> None:
    """Validate the optional cgroup object of a report result."""
    if document is None:
        return
    if not isinstance(document, dict):
        msg = f"{subject} is not an object"
        raise EvidenceFormatError(msg)
    require_exact(
        document,
        frozenset({
            "path",
            "start",
            "finish",
            "oom_kill_delta",
            "unavailable_reason",
        }),
        subject=subject,
    )
    require_str(document["path"], subject=f"{subject}.path", optional=True)
    require_optional_int(
        document["oom_kill_delta"], subject=f"{subject}.oom_kill_delta"
    )
    require_str(
        document["unavailable_reason"],
        subject=f"{subject}.unavailable_reason",
        optional=True,
    )
    for side in ("start", "finish"):
        memory = document[side]
        if memory is None:
            continue
        if not isinstance(memory, dict):
            msg = f"{subject}.{side} is not an object"
            raise EvidenceFormatError(msg)
        require_exact(
            memory,
            frozenset({"current", "peak", "oom_kill", "oom", "max_events"}),
            subject=f"{subject}.{side}",
        )
        for field in ("current", "peak", "oom_kill", "oom", "max_events"):
            decode_measurement(memory[field], subject=f"{subject}.{side}.{field}")
