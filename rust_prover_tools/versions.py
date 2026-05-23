"""Version, checksum, and toolchain parsing helpers.

The module contains pure helpers for reading version pins, validating semantic
versions, extracting tool versions from command output, looking up release
checksums, and parsing the Rust toolchain required by Verus.

Typical usage is to call `read_version_pin` for a Kani pin, use
`parse_tool_version` on `cargo kani --version` output, call `expected_sha256`
for a Verus release archive, and parse `verus --version` output with
`parse_verus_toolchain`. Helpers return strings and raise `ProverToolError` for
missing, empty, malformed, or unparseable inputs.
"""

from __future__ import annotations

import re
import typing as typ

from rust_prover_tools.errors import ProverToolError

if typ.TYPE_CHECKING:
    from pathlib import Path

MIN_CHECKSUM_FIELDS = 2
SEMVER_PATTERN = re.compile(r"^[0-9]+[.][0-9]+[.][0-9]+$")
SEMVER_SEARCH = re.compile(r"([0-9]+[.][0-9]+[.][0-9]+)")
TOOLCHAIN_LINE = re.compile(r"^Toolchain:[ \t]*(?P<toolchain>\S+)", re.MULTILINE)
REQUIRED_TOOLCHAIN = re.compile(
    r"required rust toolchain[ \t]+(?P<toolchain>\S+)",
)

__all__ = (
    "expected_sha256",
    "parse_tool_version",
    "parse_verus_toolchain",
    "read_required_file",
    "read_version_pin",
    "validate_semver",
)


def read_required_file(path: Path, *, description: str) -> str:
    """Read a required UTF-8 text file.

    Parameters
    ----------
    path : Path
        File path to read.
    description : str
        Human-readable description for error messages.

    Returns
    -------
    str
        The file contents with surrounding whitespace removed.

    Raises
    ------
    ProverToolError
        Raised when the file does not exist, cannot be read, is not UTF-8, or
        is empty after stripping.
    """
    if not path.is_file():
        msg = f"{description} '{path}' does not exist"
        raise ProverToolError(msg)
    try:
        value = path.read_text(encoding="utf-8").strip()
    except UnicodeDecodeError as exc:
        msg = f"{description} '{path}' is not valid UTF-8"
        raise ProverToolError(msg) from exc
    except OSError as exc:
        msg = f"failed to read {description} '{path}': {exc}"
        raise ProverToolError(msg) from exc
    if not value:
        msg = f"{description} '{path}' is empty"
        raise ProverToolError(msg)
    return value


def validate_semver(version: str) -> str:
    """Return `version` when it uses `MAJOR.MINOR.PATCH` format.

    Parameters
    ----------
    version : str
        Version string to validate against `SEMVER_PATTERN`.

    Returns
    -------
    str
        The validated version string.

    Raises
    ------
    ProverToolError
        Raised when `SEMVER_PATTERN.fullmatch(version)` fails.
    """
    if not SEMVER_PATTERN.fullmatch(version):
        msg = f"version pin '{version}' must use MAJOR.MINOR.PATCH format"
        raise ProverToolError(msg)
    return version


def read_version_pin(path: Path) -> str:
    """Read and validate a `MAJOR.MINOR.PATCH` version pin.

    Parameters
    ----------
    path : Path
        Path to the version pin file.

    Returns
    -------
    str
        Validated semantic version returned by `validate_semver`.

    Raises
    ------
    ProverToolError
        Raised when `read_required_file` or `validate_semver` rejects the pin.
    """
    return validate_semver(read_required_file(path, description="version pin"))


def parse_tool_version(output: str, *, tool_name: str) -> str:
    """Parse the first semantic version from tool output.

    Parameters
    ----------
    output : str
        Text emitted by the tool.
    tool_name : str
        Tool name used in error messages.

    Returns
    -------
    str
        First semantic version matched by `SEMVER_SEARCH`.

    Raises
    ------
    ProverToolError
        Raised when `SEMVER_SEARCH` finds no version.
    """
    match = SEMVER_SEARCH.search(output)
    if match is None:
        msg = f"could not parse {tool_name} version from: {output}"
        raise ProverToolError(msg)
    return match.group(1)


def expected_sha256(checksum_file: Path, archive_name: str) -> str:
    """Return the SHA-256 checksum for an archive listed in a checksum file.

    Parameters
    ----------
    checksum_file : Path
        Checksum file to scan.
    archive_name : str
        Archive file name to match in the second column.

    Returns
    -------
    str
        Expected SHA-256 checksum string.

    Raises
    ------
    ProverToolError
        Raised when the checksum file is missing, empty, or lacks the archive.
    """
    text = read_required_file(checksum_file, description="checksum file")
    for line in text.splitlines():
        fields = line.split()
        if len(fields) >= MIN_CHECKSUM_FIELDS and fields[1] == archive_name:
            return fields[0]
    msg = f"missing SHA-256 for {archive_name} in {checksum_file}"
    raise ProverToolError(msg)


def parse_verus_toolchain(output: str) -> str:
    """Parse the Rust toolchain required by `verus --version` output.

    Parameters
    ----------
    output : str
        Standard output and error text from `verus --version`.

    Returns
    -------
    str
        Parsed Rust toolchain identifier.

    Raises
    ------
    ProverToolError
        Raised when neither supported Verus toolchain pattern matches.
    """
    line_match = TOOLCHAIN_LINE.search(output)
    if line_match is not None:
        return line_match.group("toolchain")
    required_match = REQUIRED_TOOLCHAIN.search(output)
    if required_match is not None:
        return required_match.group("toolchain")
    msg = f"failed to parse Verus toolchain from output:\n{output}"
    raise ProverToolError(msg)
