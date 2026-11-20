"""Name the grammar of one Kani log and the small facts inside it.

Every regular expression, literal marker and accepted status the parser
recognises lives here, together with the two leaf value types a single report
line can produce.  Keeping the grammar separate means a new Kani release can be
compared against the vocabulary without reading the cursor logic.

The grammar was read from the committed corpus in
``tests/fixtures/kani_logs/<version>`` rather than from Kani's documentation,
so it describes what the pinned release actually printed.  A Kani log interleaves
three layers: the driver's banner and ``[Kani] Running:`` commands, the Cargo
noise around the build, and the regular-format report (``Checking harness``,
``RESULTS:``, ``SUMMARY:`` and ``VERIFICATION:``).  Only the third layer carries
verdicts.

Examples
--------
>>> release = SUPPORTED_KANI_VERSIONS[0]
>>> BANNER.match(f"Kani Rust Verifier {release} (cargo plugin)")["version"] == release
True
>>> PropertyResult("harnesses::x.cover.1", "SATISFIED", "x").is_cover
True
"""

from __future__ import annotations

import dataclasses as dc
import re

from rust_prover_tools.errors import ProverToolError

__all__ = (
    "BANNER",
    "SUPPORTED_KANI_VERSIONS",
    "FailedCheck",
    "KaniLogError",
    "PropertyResult",
)

#: Kani versions whose verbose output this parser has been qualified against.
#: The release pin lives in :data:`rust_prover_tools.binary_assets.\
#: PREBUILT_KANI_VERSION`; a new release is added here only after a real corpus
#: for it has been captured.
SUPPORTED_KANI_VERSIONS: tuple[str, ...] = ("0.67.0",)

BANNER = re.compile(
    r"^Kani Rust Verifier (?P<version>\d+\.\d+\.\d+) \((?P<kind>[^)]+)\)$",
)
# The harness name is whatever Kani printed, which may contain non-ASCII
# characters.  Only the trailing ellipsis is fixed.
HARNESS_START = re.compile(r"^Checking harness (?P<name>.+?)\.\.\.$")
VERIFICATION = re.compile(
    r"^VERIFICATION:- (?P<verdict>SUCCESSFUL|FAILED)(?: (?P<note>\(.*\)))?$",
)
VERIFICATION_TIME = re.compile(r"^Verification Time: \d+(?:\.\d+)?s$")
# CBMC names a check ``<owner>.<property>.<ordinal>``, where the ordinal counts
# the checks that property produced.  Requiring it keeps a line that merely
# begins like a check from being read as one and stopping the block early.
CHECK = re.compile(r"^Check (?P<number>\d+): (?P<identifier>\S+\.\d+)$")
FIELD = re.compile(
    r"^\t - (?P<key>Status|Description|Location): (?P<value>.*)$",
)
# Kani 0.67.0 joins several counts with a bare comma and no space, as in
# ``(1 undetermined,2 unreachable)``.  The optional space also accepts the
# spaced form, so a release that adds one does not break the parser.
_STATUS_COUNTS = r"\d+ [a-z]+(?:, ?\d+ [a-z]+)*"
TOTAL = re.compile(
    r"^ \*\* (?P<failed>\d+) of (?P<total>\d+) failed"
    rf"(?: \((?P<counts>{_STATUS_COUNTS})\))?$",
)
COVER = re.compile(
    r"^ \*\* (?P<satisfied>\d+) of (?P<total>\d+) cover properties satisfied"
    rf"(?: \((?P<counts>{_STATUS_COUNTS})\))?$",
)
COMPLETE = re.compile(
    r"^Complete - (?P<verified>\d+) successfully verified harnesses, "
    r"(?P<failures>\d+) failures, (?P<total>\d+) total\.$",
)
FAILED_FOR = re.compile(r"^Verification failed for - (?P<name>\S+)$")
FAILED_CHECK = re.compile(r"^Failed Checks: (?P<message>.*)$")
FAILED_LOCATION = re.compile(
    r'^ File: "(?P<path>[^"]+)", line (?P<line>\d+), in (?P<function>\S+)$',
)
UNSUPPORTED_ENTRY = re.compile(r"^ +- (?P<name>[A-Za-z0-9_:]+) \(\d+\)$")

LIST_WRITTEN = "Wrote list results to "
UNSUPPORTED_PRELUDE = "warning: Found the following unsupported constructs:"
UNWINDING_FAILED = (
    "[Kani] info: Verification output shows one or more unwinding failures."
)
UNSUPPORTED_REACHABLE = (
    "** WARNING: A Rust construct that is not currently supported by Kani was "
    "found to be reachable. Check the results for more details."
)
# The driver echoes the solver command it runs.  It is the only place a log
# records what CBMC was asked for, so the decision reads it rather than
# inferring the setting from a failure.  Kani does not pass
# `--unwinding-assertions` at all: it disables the assertions with
# `--no-unwinding-assertions` when the checks are off and otherwise relies on
# CBMC's default, so the presence of that one flag decides the setting.  The
# echo is framed by a fixed prefix and a closing backtick, both of which are
# removed before the command is split into arguments.
SOLVER_FRAME = "[Kani] Running: `"
SOLVER_COMMAND = f"{SOLVER_FRAME}cbmc "
NO_UNWINDING_ASSERTIONS_FLAG = "--no-unwinding-assertions"
TIMED_OUT = "CBMC timed out."
CBMC_FAILED = "CBMC failed"
# Kani's own wording when it reaches a construct it cannot model.  The corpus's
# ``unsupported`` log fails the harness's assertion with this phrase rather than
# an ``unsupported_construct`` check, so the phrase is the only distinguishing
# evidence for that shape.
UNSUPPORTED_MARKER = "not currently supported by kani"
# Kani omits ``Location`` when CBMC supplied neither a file nor a function, so
# only the two fields every check carries are required.
CHECK_FIELDS = ("Status", "Description")
CHECK_STATUSES = frozenset(
    {
        "SUCCESS",
        "FAILURE",
        "UNDETERMINED",
        "UNREACHABLE",
        "SATISFIED",
        "UNSATISFIABLE",
    },
)
# Map the word Kani prints in parentheses to the status the checks carry.  The
# same words qualify a cover count, which is why the cover parser reads them
# from here rather than keeping a second vocabulary.
OTHER_STATUS = {"undetermined": "UNDETERMINED", "unreachable": "UNREACHABLE"}
COVER_STATUSES = frozenset(OTHER_STATUS)
# The statuses a cover property can carry.  Kani's renderer turns a cover
# CBMC reported as ``FAILURE`` into ``SATISFIED`` before printing, so a cover
# line carrying ``FAILURE`` is a report Kani cannot have produced.
COVER_PROPERTY_STATUSES = frozenset(
    {"SATISFIED", "UNSATISFIABLE", "UNREACHABLE", "UNDETERMINED"},
)
COVER_KIND = "cover"
UNWINDING_KIND = "unwind"
UNSUPPORTED_KIND = "unsupported_construct"
# A check identifier is ``<owner>.<property>.<ordinal>``.  Three segments are
# what let the middle one be read as a property of a named harness; two are
# what a property needs to keep its own name and ordinal.
_ATTRIBUTED_SEGMENTS = 3
_PROPERTY_SEGMENTS = 2
NOISE_PREFIXES = (
    "[Kani] ",
    "warning: ",
    "   ",
    "    ",
    "Finished ",
    "Reading ",
    "Writing ",
    "Adding ",
    "Removing ",
    "Cleaning ",
    "Function ",
    "Virtual ",
    "Trying ",
    "Generating ",
    "Running ",
    "Passing ",
    "Converting ",
    "Solving ",
    "SAT ",
    "Generic ",
    "CBMC ",
    "Runtime ",
    "size of ",
    "slicing ",
    "Generated ",
    "generate function bodies: ",
    "aborting path on assume",
    "Unwinding loop ",
    "Not unwinding loop ",
    "Removal of ",
    "---",
    "INFO ",
    "======",
    "Total #",
    "  - ",
    "Reachable Items:",
)


class KaniLogError(ProverToolError):
    """A Kani log whose structure the parser cannot account for."""

    def __init__(self, reason: str) -> None:
        """Record the structural reason, which every caller must report.

        Parameters
        ----------
        reason : str
            Why the log could not be read as a complete Kani report.

        """
        super().__init__(f"structural error: {reason}")
        self.reason = reason


@dc.dataclass(frozen=True, slots=True)
class PropertyResult:
    """One ``Check N:`` entry from a Kani report.

    The ``Location`` field is accepted but optional and its value is not kept.
    Kani omits it when CBMC supplied no source location, and nothing reads it:
    the locations a caller can cite come from the `` File:`` lines beneath
    failures, which :class:`FailedCheck` keeps.
    """

    identifier: str
    status: str
    description: str

    @property
    def kind(self) -> str:
        """Name the check class from its identifier suffix.

        Kani ends a check identifier with the property it reports, so
        ``harnesses::x.unwind.0`` is an unwinding check and
        ``harnesses::x.cover.1`` a cover, whatever the status says.  CBMC may
        omit the owner entirely - ``cover.1`` and ``unwind.1`` - so two
        segments are enough to name the kind.  Classifying by identifier rather
        than description keeps a seeded mutation from relabelling a check by
        editing prose.

        Returns
        -------
        str
            The property kind, or an empty string when the identifier has too
            few dot-separated parts to name one.

        """
        parts = self.identifier.split(".")
        return parts[-2] if len(parts) >= _PROPERTY_SEGMENTS else ""

    @property
    def is_cover(self) -> bool:
        """Report whether this check is a declared cover property."""
        return self.kind == COVER_KIND

    @property
    def is_unwinding(self) -> bool:
        """Report whether this check is an unwinding assertion."""
        return self.kind == UNWINDING_KIND

    @property
    def owner(self) -> str | None:
        """The harness this check belongs to, or ``None`` when unnamed."""
        parts = self.identifier.split(".")
        return ".".join(parts[:-2]) if len(parts) >= _ATTRIBUTED_SEGMENTS else None

    @property
    def property(self) -> str:
        """Name this property within its owner, such as ``assertion.1``.

        Returns
        -------
        str
            The last two identifier segments, or the whole identifier when it
            has fewer than two segments.

        """
        parts = self.identifier.split(".")
        if len(parts) < _PROPERTY_SEGMENTS:
            return self.identifier
        return ".".join(parts[-2:])


@dc.dataclass(frozen=True, slots=True)
class FailedCheck:
    """One ``Failed Checks:`` entry and the location Kani attributes it to."""

    message: str
    path: str | None
    line: int | None
