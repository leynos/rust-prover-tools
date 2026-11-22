"""Name the grammar of one Kani 0.67.0 log and the facts inside it.

This is the vocabulary half of :mod:`rust_prover_tools.kani_log`: every regular
expression, literal marker, and accepted status the parser recognises,
together with the two leaf value types a single line can produce. Keeping the
grammar apart from the cursor logic means a new Kani release can be compared
against it without reading the state machine.

The grammar was read from the committed corpus in ``tests/fixtures/kani_logs``,
which was captured from Kani 0.67.0, rather than from Kani's documentation, so
it describes what the pinned release actually printed. A Kani log interleaves
three layers: the driver's banner and ``[Kani] Running:`` commands, the Cargo
noise around the build, and the regular-format report
(``Checking harness`` / ``RESULTS:`` / ``SUMMARY:`` / ``VERIFICATION:``). Only
the third layer carries verdicts.

The grammar itself is derived from Combobulate's ``docs_validation.kani_log``
package at commit ``9dd6e3c`` and is used under the ISC licence recorded in
``ATTRIBUTION.md`` beside this module.

Examples
--------
>>> BANNER.match("Kani Rust Verifier 0.67.0 (cargo plugin)")["version"]
'0.67.0'
>>> PropertyResult("harnesses::x.cover.1", "SATISFIED", "x").is_cover
True
"""

from __future__ import annotations

import re
import typing as typ

__all__ = (
    "ASSERTION_KIND",
    "BANNER",
    "CBMC_FAILED",
    "CHECK",
    "CHECK_FIELDS",
    "CHECK_STATUSES",
    "COMPLETE",
    "COVER",
    "COVER_KIND",
    "FAILED_CHECK",
    "FAILED_FOR",
    "FAILED_LOCATION",
    "FIELD",
    "HARNESS_START",
    "LIST_WRITTEN",
    "NOISE_PREFIXES",
    "NO_UNWINDING_ASSERTIONS_FLAG",
    "OTHER_STATUS",
    "SOLVER_COMMAND",
    "TIMED_OUT",
    "TOTAL",
    "UNSUPPORTED_ENTRY",
    "UNSUPPORTED_KIND",
    "UNSUPPORTED_MARKER",
    "UNSUPPORTED_PRELUDE",
    "UNSUPPORTED_REACHABLE",
    "UNWINDING_BOUND_FLAGS",
    "UNWINDING_FAILED",
    "UNWINDING_KIND",
    "VERIFICATION",
    "VERIFICATION_TIME",
    "FailedCheck",
    "KaniLogError",
    "PropertyResult",
    "UnsupportedKaniGrammarError",
    "is_noise",
)

type LineStreamName = typ.Literal["stdout", "stderr"]

BANNER = re.compile(
    r"^Kani Rust Verifier (?P<version>\d+\.\d+\.\d+) \((?P<kind>[^)]+)\)$"
)
HARNESS_START = re.compile(r"^Checking harness (?P<name>.+)\.\.\.$")
VERIFICATION = re.compile(
    r"^VERIFICATION:- (?P<verdict>SUCCESSFUL|FAILED)(?: (?P<note>\(.*\)))?$"
)
VERIFICATION_TIME = re.compile(r"^Verification Time: \d+(?:\.\d+)?s$")
CHECK = re.compile(r"^Check (?P<number>\d+): (?P<identifier>\S+)$")
FIELD = re.compile(r"^\t - (?P<key>Status|Description|Location): (?P<value>.*)$")
TOTAL = re.compile(
    r"^ \*\* (?P<failed>\d+) of (?P<total>\d+) failed"
    r"(?: \((?P<others>[^)]*)\))?$"
)
COVER = re.compile(
    r"^ \*\* (?P<satisfied>\d+) of (?P<total>\d+) cover properties satisfied"
    r"(?: \((?P<others>[^)]*)\))?$"
)
COMPLETE = re.compile(
    r"^Complete - (?P<verified>\d+) successfully verified harnesses, "
    r"(?P<failures>\d+) failures, (?P<total>\d+) total\.$"
)
FAILED_FOR = re.compile(r"^Verification failed for - (?P<name>\S+)$")
FAILED_CHECK = re.compile(r"^Failed Checks: (?P<message>.*)$")
FAILED_LOCATION = re.compile(
    r'^ File: "(?P<path>[^"]+)", line (?P<line>\d+), in (?P<function>\S+)$'
)
UNSUPPORTED_ENTRY = re.compile(r"^ +- (?P<name>[A-Za-z0-9_:]+) \(\d+\)$")

LIST_WRITTEN = "Wrote list results to "
UNSUPPORTED_PRELUDE = "warning: Found the following unsupported constructs:"
UNWINDING_FAILED = (
    "[Kani] info: Verification output shows one or more unwinding failures."
)
UNSUPPORTED_REACHABLE = (
    "** WARNING: A Rust construct that is not currently supported by Kani was found "
    "to be reachable. Check the results for more details."
)
# The driver echoes the solver command it runs. It is the only place a log
# records what CBMC was asked for, so the parser keeps it rather than inferring
# the setting from a failure. A bound is what makes CBMC emit unwinding
# assertions, and the pinned driver disables them explicitly rather than passing
# `--unwinding-assertions`.
SOLVER_COMMAND = "[Kani] Running: `cbmc "
UNWINDING_BOUND_FLAGS = (" --unwind ", " --unwindset ")
NO_UNWINDING_ASSERTIONS_FLAG = " --no-unwinding-assertions"
TIMED_OUT = "CBMC timed out."
CBMC_FAILED = "CBMC failed"
# Kani's own wording when it reaches a construct it cannot model. A failed
# assertion carrying this phrase is `unsupported`, not a counterexample.
UNSUPPORTED_MARKER = "not currently supported by kani"
CHECK_FIELDS = ("Status", "Description", "Location")
CHECK_STATUSES = frozenset({
    "SUCCESS",
    "FAILURE",
    "UNDETERMINED",
    "UNREACHABLE",
    "SATISFIED",
    "UNSATISFIABLE",
})
# Map the word Kani prints in parentheses to the status the checks carry.
OTHER_STATUS = {"undetermined": "UNDETERMINED", "unreachable": "UNREACHABLE"}
ASSERTION_KIND = "assertion"
# A property identifier is ``harness::fn.kind.N``; fewer than three
# dot-separated parts name no harness, and fewer than two name no property.
_MIN_IDENTIFIER_PARTS = 3
_MIN_PROPERTY_PARTS = 2
COVER_KIND = "cover"
UNWINDING_KIND = "unwind"
UNSUPPORTED_KIND = "unsupported_construct"
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


class KaniLogError(ValueError):
    """A log the parser cannot account for.

    Attributes
    ----------
    reason : str
        Structural reason without the ``structural error:`` prefix. Callers
        report this verbatim, because it names the construct the grammar
        wanted rather than an offset in a string.
    """

    def __init__(self, reason: str) -> None:
        """Record the structural reason every caller must report."""
        super().__init__(f"structural error: {reason}")
        self.reason = reason


class UnsupportedKaniGrammarError(KaniLogError):
    """A log from a Kani release this parser was never written for.

    Attributes
    ----------
    version : str
        Release named by the banner.
    supported : tuple[str, ...]
        Releases the registry can parse, in sorted order.
    """

    def __init__(self, version: str, supported: tuple[str, ...]) -> None:
        """Record the refused release and the registered releases."""
        super().__init__(
            f"the log is from Kani {version}, but this parser reads {list(supported)}"
        )
        self.version = version
        self.supported = supported


@typ.final
class PropertyResult(typ.NamedTuple):
    """One ``Check N:`` entry from a harness block.

    Attributes
    ----------
    identifier : str
        Full property identifier, such as ``harnesses::x.cover.1``.
    status : str
        One of ``CHECK_STATUSES``.
    description : str
        Kani's description with surrounding quotes removed.
    """

    identifier: str
    status: str
    description: str

    @property
    def kind(self) -> str:
        """Name the check class from its identifier suffix.

        Kani ends an identifier with the property it reports, so
        ``harnesses::x.unwind.0`` is an unwinding check and
        ``harnesses::x.cover.1`` a witness, whatever the status says.
        Classifying by identifier rather than description keeps a seeded
        mutation from relabelling a check by editing prose.
        """
        parts = self.identifier.split(".")
        return parts[-2] if len(parts) >= _MIN_IDENTIFIER_PARTS else ""

    @property
    def is_cover(self) -> bool:
        """Report whether this check is a declared success witness."""
        return self.kind == COVER_KIND

    @property
    def is_unwinding(self) -> bool:
        """Report whether this check is an unwinding assertion."""
        return self.kind == UNWINDING_KIND

    @property
    def owner(self) -> str | None:
        """The harness this check belongs to, or `None` when it names none."""
        parts = self.identifier.split(".")
        return ".".join(parts[:-2]) if len(parts) >= _MIN_IDENTIFIER_PARTS else None

    @property
    def property(self) -> str:
        """Name the property within its harness, such as ``assertion.1``."""
        parts = self.identifier.split(".")
        return (
            ".".join(parts[-2:])
            if len(parts) >= _MIN_PROPERTY_PARTS
            else self.identifier
        )


@typ.final
class FailedCheck(typ.NamedTuple):
    """One ``Failed Checks:`` entry and its optional source location.

    Attributes
    ----------
    message : str
        Kani's failure message.
    path : str | None
        Source path from the `` File:`` line, or `None` when it is missing.
    line : int | None
        One-based source line, or `None` when the location is missing.
    """

    message: str
    path: str | None
    line: int | None


def is_noise(line: str) -> bool:
    """Report whether a line is build or driver chatter, not report content.

    The prefixes are deliberately broad: the parser refuses logs it cannot
    explain *inside* a report, and refusing a new Cargo message between reports
    would make it break on every toolchain bump.
    """
    return not line.strip() or line.startswith(NOISE_PREFIXES)
