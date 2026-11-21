"""Name the grammar of one captured Verus run and its error type.

Every regular expression and marker the parser recognizes lives here so a new
Verus release can be compared against the vocabulary without reading the scan
logic.  The grammar was read from the committed corpus in
``tests/fixtures/verus_logs/<version>``, produced by the release named there,
rather than from Verus documentation.

A Verus run writes to two streams: the ``verification results::`` summary goes
to standard output and every diagnostic goes to standard error.  A captured
log therefore interleaves them in whatever order the capture merged them, so
nothing here may depend on the summary coming last.
"""

from __future__ import annotations

import re

from rust_prover_tools.errors import ProverToolError

__all__ = (
    "ERROR_HEADER",
    "FULL_RUN",
    "LOCATION",
    "PARTIAL_RUN",
    "SELECTION_NOTE",
    "SUMMARY",
    "SUPPORTED_VERUS_VERSIONS",
    "TRAILER_PREFIXES",
    "VerusLogError",
)

#: Verus releases whose captured output this parser has been qualified
#: against.  A new release is added here only after a real corpus for it has
#: been captured; the evidence for each entry is the release-named directory
#: under ``tests/fixtures/verus_logs/`` described in its ``README.md``.
SUPPORTED_VERUS_VERSIONS: tuple[str, ...] = (
    "0.2026.11.02.8bf68ee",
    "0.2026.09.06.8dea4a2",
)

FULL_RUN = "full"
PARTIAL_RUN = "partial"

# The summary Verus prints once per invocation.  The parenthesized marker is
# present only when the run selected part of the crate with ``--verify-*``, and
# the backticks and asterisk are part of the text Verus prints.
SUMMARY = re.compile(
    r"^verification results:: (?P<verified>\d+) verified, "
    r"(?P<errors>\d+) errors"
    r"(?P<partial> \(partial verification with `--verify-\*`\))?$",
)
# A diagnostic header.  rustc adds a bracketed code for compiler errors
# (``error[E0308]:``); Verus adds none for verification failures.  Either way
# the message is what follows the colon.
#
# The driver's own option errors are capitalised (``Error: --verify-function
# option requires ...``) and carry no code.  Both releases in the corpus print
# that form when Verus refuses to start, and skipping it as chatter would
# report a failed invocation with no reason for the failure.
ERROR_HEADER = re.compile(
    r"^(?P<severity>[Ee]rror)(?:\[(?P<code>[A-Za-z0-9]+)\])?: (?P<message>.+)$",
)
# rustc prints the span one level in, with two leading spaces and an arrow.
LOCATION = re.compile(
    r"^\s*--> (?P<path>.+?):(?P<line>\d+):(?P<column>\d+)$",
)
# The note that names what was selected for verification.  Verus prints the
# unbracketed form for a module-level selection and appends ``(selected
# functions)`` when ``--verify-function`` narrowed it further.
SELECTION_NOTE = re.compile(r"^note: verifying (?P<subject>.+)$")
# rustc's own closing lines are diagnostics by shape but say nothing about what
# failed, so the parser keeps them out of the failure list.
TRAILER_PREFIXES = ("error: aborting due to ", "error: could not compile ")


class VerusLogError(ProverToolError):
    """A captured Verus log whose structure the parser cannot account for."""

    def __init__(self, reason: str) -> None:
        """Record the structural reason, which every caller must report.

        Parameters
        ----------
        reason : str
            Why the log could not be read as one Verus run's report.

        """
        super().__init__(f"structural error: {reason}")
        self.reason = reason
