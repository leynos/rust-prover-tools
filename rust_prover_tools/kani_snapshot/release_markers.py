"""The run and build-input markers a release body carries.

A draft may be resumed only by the run and attempt that created it, and may be
published as a no-op only by a run with the same build inputs. Both facts are
written into the release body, so an observer can read them back without
downloading assets, and so they survive publication unchanged. `release_body`
writes them and the parsers return `None` rather than a default when a body
carries no well-formed marker: `None` means "cannot prove", never "attempt
zero" or "assume equal".
"""

from __future__ import annotations

import re
import typing as typ

__all__ = ("parse_input_marker", "parse_run_marker", "release_body")


#: Digits allowed in one marker number. A 64-bit workflow identifier is at
#: most 20 digits, and a shorter cap keeps `int` well inside the interpreter's
#: integer-string conversion limit, so an absurd marker is refused by the
#: pattern instead of raising from the conversion.
_MAX_MARKER_DIGITS: typ.Final[int] = 20

#: Machine-readable record of the run that created a draft.
_RUN_MARKER: typ.Final[re.Pattern[str]] = re.compile(
    rf"<!-- kani-snapshot-run: (?P<run_id>[1-9]\d{{0,{_MAX_MARKER_DIGITS - 1}}})"
    rf"/(?P<run_attempt>[1-9]\d{{0,{_MAX_MARKER_DIGITS - 1}}}) -->"
)

#: Machine-readable record of the build inputs a release was created from.
_INPUT_MARKER: typ.Final[re.Pattern[str]] = re.compile(
    r"<!-- kani-snapshot-input: (?P<fingerprint>[0-9a-f]{64}) -->"
)


def release_body(
    tag: str,
    fingerprint: str,
    *,
    run_id: int,
    run_attempt: int,
) -> str:
    """Return the release body that records a release's origin.

    A draft may be resumed only by the run and attempt that created it, and may
    be published as a no-op only by a run with the same build inputs. Both
    facts are written into the body so an observer can read them back without
    downloading assets, and so they survive publication unchanged.

    Parameters
    ----------
    tag : str
        Immutable snapshot tag the release publishes.
    fingerprint : str
        SHA-256 over the declared build inputs. It binds a no-op to equal
        inputs, not to equal output bytes.
    run_id : int
        Trusted workflow run that created the draft.
    run_attempt : int
        Attempt of that run.

    Returns
    -------
    str
        Release body naming the tag and carrying one input marker and one run
        marker in the form the parsers below accept.
    """
    return (
        f"Immutable Kani snapshot {tag}\n\n"
        f"<!-- kani-snapshot-input: {fingerprint} -->\n"
        f"<!-- kani-snapshot-run: {run_id}/{run_attempt} -->\n"
    )


def parse_run_marker(body: str | None) -> tuple[int, int] | None:
    """Return the run and attempt a release body records, when it does.

    Parameters
    ----------
    body : str | None
        Release body read from the API. `None` means the API reported no body
        at all.

    Returns
    -------
    tuple[int, int] | None
        `(run_id, run_attempt)` from the run marker, or `None` when the body
        is absent or carries no well-formed marker. `None` therefore means
        "cannot prove which attempt created this release", never attempt zero.
    """
    if body is None:
        return None
    match = _RUN_MARKER.search(body)
    if match is None:
        return None
    return int(match.group("run_id")), int(match.group("run_attempt"))


def parse_input_marker(body: str | None) -> str | None:
    """Return the build-input fingerprint a release body records, when it does.

    Parameters
    ----------
    body : str | None
        Release body read from the API. `None` means the API reported no body
        at all.

    Returns
    -------
    str | None
        The fingerprint from the input marker, or `None` when the body is
        absent or carries no well-formed marker. `None` means the release
        cannot name its inputs, so it is never assumed to equal this run's.
    """
    if body is None:
        return None
    match = _INPUT_MARKER.search(body)
    return None if match is None else match.group("fingerprint")
