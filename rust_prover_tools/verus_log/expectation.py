"""Declare where a deliberately broken proof is expected to fail.

A negative control - a seeded mutation, a broken lemma, a disabled check - only
demonstrates that verification would notice the fault if the failure lands on
the fault.  A run that instead fails somewhere else shows nothing, so the
expectation names a file and, optionally, a range of lines within it.

The range exists because Verus reports a postcondition failure at the contract
line above the body rather than inside it, which is rarely the line a mutation
edit touched.
"""

from __future__ import annotations

import dataclasses as dc
import pathlib
import re
import typing as typ

if typ.TYPE_CHECKING:
    from rust_prover_tools.verus_log.model import VerusFailure

__all__ = ("FailureExpectation", "parse_expectation")

#: The optional ``:START[-END]`` suffix of an expectation.  The path is lazy so
#: a directory containing digits or dashes keeps them; only a trailing line
#: range is read as one.
_EXPECTATION = re.compile(
    r"^(?P<path>.+?):(?P<start>\d+)(?:-(?P<end>\d+))?$",
)


def _normalize(path: str, *, windows: bool) -> str:
    """Return a comparable form of a path Verus printed or a caller typed.

    The comparison ignores spellings that cannot change which file a path
    names: a ``.`` component, an empty component, a trailing separator, and —
    on a host that spells paths with Windows separators — a separator written
    the other way round.  The caller supplies the flavour: a POSIX name may
    contain a literal backslash, which names a different file from a
    slash-separated one, while a Windows path accepts either separator.  The
    selector is a parameter so this module stays a pure function of its
    arguments; the CLI adapter decides which host captured the log.

    A ``..`` component is kept.  It is resolved against the component before
    it, and when that component is a symbolic link, ``linked/../mutant.rs``
    names a different file from ``mutant.rs``; collapsing it would let one
    diagnostic satisfy an expectation for a file the run never read.  Nothing
    is resolved against the filesystem either: the log and the expectation may
    describe a machine that is no longer present.
    """
    flavour = pathlib.PureWindowsPath if windows else pathlib.PurePosixPath
    return flavour(path).as_posix()


@dc.dataclass(frozen=True, slots=True)
class FailureExpectation:
    """The place a negative-control run is required to fail."""

    path: str
    first_line: int | None = None
    last_line: int | None = None
    #: Whether a backslash separates components, as on Windows.  The default is
    #: POSIX, where it is an ordinary filename character.
    windows_paths: bool = False

    def contains(self, failure: VerusFailure) -> bool:
        """Report whether one diagnostic lands inside the expected region.

        Parameters
        ----------
        failure : VerusFailure
            A diagnostic parsed from the log.

        Returns
        -------
        bool
            ``True`` when the diagnostic names this file at a line inside the
            declared range.  An unlocated diagnostic never matches, and a
            path-only expectation accepts any line in that file.

        """
        if not failure.is_located or failure.path is None:
            return False
        if _normalize(failure.path, windows=self.windows_paths) != _normalize(
            self.path,
            windows=self.windows_paths,
        ):
            return False
        if self.first_line is None:
            return True
        if failure.line is None:
            return False
        return self.first_line <= failure.line <= (self.last_line or self.first_line)

    def describe(self) -> str:
        """Render the expectation the way a caller wrote it.

        Returns
        -------
        str
            ``path``, ``path:line`` or ``path:start-end``.

        """
        if self.first_line is None:
            return self.path
        if self.last_line is None:
            return f"{self.path}:{self.first_line}"
        return f"{self.path}:{self.first_line}-{self.last_line}"


def parse_expectation(
    text: str,
    *,
    windows_paths: bool = False,
) -> FailureExpectation:
    """Read one ``path[:start[-end]]`` expectation from command-line text.

    Parameters
    ----------
    text : str
        The value a caller supplied.
    windows_paths : bool
        Whether a backslash separates path components, as on Windows.  The CLI
        adapter decides this from the host that captured the log.

    Returns
    -------
    FailureExpectation
        The parsed region.  A text with no trailing line range is a path-only
        expectation.

    Raises
    ------
    ValueError
        If the text is empty, the range counts from zero, or the range ends
        before it starts.

    Examples
    --------
    >>> parse_expectation("proofs/mutant.rs:12-18").describe()
    'proofs/mutant.rs:12-18'
    >>> parse_expectation("proofs/mutant.rs").describe()
    'proofs/mutant.rs'
    """
    stripped = text.strip()
    if not stripped:
        msg = "the failure expectation is empty"
        raise ValueError(msg)
    match = _EXPECTATION.match(stripped)
    if match is None:
        return FailureExpectation(stripped, windows_paths=windows_paths)
    start_text = match["start"]
    start = int(start_text)
    end_text = match["end"]
    end = int(end_text) if end_text is not None else start
    if start < 1 or end < start:
        msg = f"the failure expectation {text!r} is not a line range"
        raise ValueError(msg)
    # A single line keeps no end, so an expectation written ``path:8`` is not
    # reported back as the range ``path:8-8``.
    return FailureExpectation(
        match["path"],
        start,
        end if end_text is not None and end != start else None,
        windows_paths,
    )
