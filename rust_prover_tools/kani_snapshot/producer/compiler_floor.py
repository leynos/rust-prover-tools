"""The supported Rust compiler floor and the harness that demonstrates it.

A snapshot is useful only if its pinned toolchain compiles code that needs Rust
1.94. `slice::array_windows` was stabilised in 1.94, so one small proof supplies
both halves of the demonstration: the pinned compiler accepts it, and a 1.93
compiler rejects it with the unstable-feature error. Parsing the two transcripts
here keeps the claim evidence-based rather than asserted in prose.
"""

from __future__ import annotations

import re
import typing as typ

from rust_prover_tools.errors import ProverToolError

__all__ = (
    "COMPILER_FLOOR",
    "FLOOR_ERROR_CODE",
    "FLOOR_FEATURE",
    "FLOOR_HARNESS",
    "parse_rustc_version",
    "require_compiler_floor",
    "require_floor_harness_verified",
    "require_unstable_feature_rejected",
)

#: Oldest Rust release a snapshot's pinned toolchain must reach or exceed.
COMPILER_FLOOR: typ.Final[tuple[int, int]] = (1, 94)

#: The proof harness in the positive fixture that must verify.
FLOOR_HARNESS: typ.Final[str] = "bounded_adjacent_pairs_are_exact"

#: The API whose stabilisation defines the floor.
FLOOR_FEATURE: typ.Final[str] = "array_windows"

#: The compiler diagnostic that rejects the feature below the floor.
FLOOR_ERROR_CODE: typ.Final[str] = "E0658"

_RUSTC_VERSION: typ.Final[re.Pattern[str]] = re.compile(
    r"^rustc (?P<major>\d+)\.(?P<minor>\d+)\.\d+"
)
_VERIFICATION: typ.Final[re.Pattern[str]] = re.compile(
    r"^\s*VERIFICATION:- SUCCESSFUL",
    re.MULTILINE,
)
_HARNESS_START: typ.Final[re.Pattern[str]] = re.compile(
    r"^Checking harness (?P<name>\S+?)\.\.\.$",
    re.MULTILINE,
)


def parse_rustc_version(value: str) -> tuple[int, int]:
    """Return the major and minor release a `rustc --version` line names.

    Parameters
    ----------
    value : str
        First line of `rustc --version`, with or without a prerelease suffix.

    Returns
    -------
    tuple[int, int]
        Major and minor release numbers.

    Raises
    ------
    ProverToolError
        Raised when the text is not a `rustc` version line.
    """
    match = _RUSTC_VERSION.match(value.strip())
    if match is None:
        msg = f"rustc version {value!r} is not a 'rustc <major>.<minor>.<patch>' line"
        raise ProverToolError(msg)
    return (int(match.group("major")), int(match.group("minor")))


def require_compiler_floor(
    value: str,
    floor: tuple[int, int] = COMPILER_FLOOR,
) -> None:
    """Require a compiler version at or above the supported floor.

    Parameters
    ----------
    value : str
        First line of `rustc --version`.
    floor : tuple[int, int]
        Minimum supported major and minor release.

    Raises
    ------
    ProverToolError
        Raised when the compiler is older than `floor`.
    """
    version = parse_rustc_version(value)
    if version < floor:
        msg = (
            f"compiler {value.strip()!r} is below the snapshot floor "
            f"{floor[0]}.{floor[1]}"
        )
        raise ProverToolError(msg)


def _harness_sections(transcript: str) -> dict[str, list[str]]:
    """Split a Kani transcript into the text that follows each harness check.

    Kani prints `Checking harness <name>...` and then that harness's own
    verification result, so a verdict belongs to the nearest preceding harness
    line. Searching the whole transcript would let one harness's success
    discharge another harness's failed check.
    """
    starts = list(_HARNESS_START.finditer(transcript))
    sections: dict[str, list[str]] = {}
    for index, start in enumerate(starts):
        end = starts[index + 1].start() if index + 1 < len(starts) else len(transcript)
        body = transcript[start.end() : end]
        sections.setdefault(start.group("name"), []).append(body)
    return sections


def require_floor_harness_verified(
    transcript: str,
    harness: str = FLOOR_HARNESS,
) -> None:
    """Require a Kani transcript proving the floor harness verified.

    Parameters
    ----------
    transcript : str
        Combined standard output and error of one `kani` run.
    harness : str
        Proof harness the run must have checked.

    Raises
    ------
    ProverToolError
        Raised when the transcript does not check the harness, or when any run
        of it does not report a successful verification. A summary-only
        transcript cannot show which harness ran, so the harness line is
        required, and a success reported for a different harness is not
        accepted as this harness's verdict.
    """
    blocks = _harness_sections(transcript).get(harness)
    if not blocks:
        msg = f"compiler-floor transcript does not check harness {harness!r}"
        raise ProverToolError(msg)
    if any(_VERIFICATION.search(block) is None for block in blocks):
        msg = f"compiler-floor transcript does not verify harness {harness!r}"
        raise ProverToolError(msg)


def _diagnostic_messages(transcript: str, error_code: str) -> list[str]:
    """Return the compiler's diagnostic messages for one error code.

    The feature name has to appear *in* the diagnostic that rejects it. A
    transcript that reports an unrelated unstable feature and mentions the
    floor feature somewhere else would otherwise pass, and the negative
    control would no longer show that this feature needs a newer compiler.
    """
    pattern = re.compile(rf"^error\[{re.escape(error_code)}\]:.*$", re.MULTILINE)
    return pattern.findall(transcript)


def require_unstable_feature_rejected(
    transcript: str,
    error_code: str = FLOOR_ERROR_CODE,
) -> None:
    """Require the below-floor compiler to reject the floor feature.

    Parameters
    ----------
    transcript : str
        Combined standard output and error of the negative control compile.
    error_code : str
        Diagnostic code the compiler must report.

    Raises
    ------
    ProverToolError
        Raised when the transcript lacks the unstable-feature diagnostic for
        the floor feature, which would mean the negative control passed for
        some unrelated reason.
    """
    messages = _diagnostic_messages(transcript, error_code)
    if not messages:
        msg = f"compiler-floor negative control lacks diagnostic {error_code}"
        raise ProverToolError(msg)
    if not any(FLOOR_FEATURE in message for message in messages):
        msg = (
            f"compiler-floor negative control does not report {FLOOR_FEATURE!r} "
            f"as unstable in a {error_code} diagnostic"
        )
        raise ProverToolError(msg)
