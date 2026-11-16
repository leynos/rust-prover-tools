"""Parsers for the upstream files that evidence a pinned recipe.

The recipe is only trustworthy if it agrees with the pinned tree. These parsers
read the small upstream files a reviewer can also read: `rust-toolchain.toml`,
`Cargo.toml`, `kani-dependencies`, `.gitmodules` and the build tool's licence
notes. Tests compare the parsed values with the checked-in fixtures, so an
upstream change cannot silently leave the recipe stale.

The distinction that matters for licences: upstream's licence notes name Kani,
rustc and CBMC but not Kissat, even though the bundle ships a Kissat binary.
`missing_licence_evidence` therefore requires notice-listed components to
appear in the notes and requires every other redistributed component to carry
its own licence source.
"""

from __future__ import annotations

import re
import tomllib
import typing as typ

from rust_prover_tools.errors import ProverToolError
from rust_prover_tools.kani_snapshot.producer.recipe_model import (
    NOTICE_INVENTORY,
    LicenceRecord,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

__all__ = (
    "missing_licence_evidence",
    "parse_crate_version",
    "parse_dependency_versions",
    "parse_licence_inventory",
    "parse_submodule_paths",
    "parse_toolchain_channel",
)

_CHANNEL: typ.Final[re.Pattern[str]] = re.compile(r"nightly-\d{4}-\d{2}-\d{2}")
_ASSIGNMENT: typ.Final[re.Pattern[str]] = re.compile(
    r'(?P<name>[A-Z][A-Z0-9_]*)="(?P<value>[^"]*)"'
)
_SECTION: typ.Final[re.Pattern[str]] = re.compile(r'\[submodule "(?P<name>[^"]+)"\]')
_NOTICE: typ.Final[re.Pattern[str]] = re.compile(
    r"(?P<name>[A-Za-z0-9_.+-]+):\s+https?://\S+"
)


def _decode(data: bytes, field: str) -> str:
    """Decode a UTF-8 evidence file, naming it on failure."""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        msg = f"upstream {field} is not valid UTF-8: {exc}"
        raise ProverToolError(msg) from exc


def _toml(data: bytes, field: str) -> dict[str, object]:
    """Decode a TOML evidence file."""
    try:
        document = tomllib.loads(_decode(data, field))
    except (tomllib.TOMLDecodeError, RecursionError, ValueError) as exc:
        msg = f"upstream {field} is not valid TOML: {exc}"
        raise ProverToolError(msg) from exc
    return typ.cast("dict[str, object]", document)


def parse_toolchain_channel(data: bytes) -> str:
    """Return the dated nightly channel `rust-toolchain.toml` names.

    Parameters
    ----------
    data : bytes
        Exact `rust-toolchain.toml` bytes.

    Returns
    -------
    str
        The channel, such as `nightly-2026-08-21`.

    Raises
    ------
    ProverToolError
        Raised for invalid TOML, a missing `[toolchain]` table, a non-string
        channel or a channel that is not a dated nightly.
    """
    document = _toml(data, "rust-toolchain.toml")
    table = document.get("toolchain")
    if not isinstance(table, dict):
        msg = "upstream rust-toolchain.toml has no [toolchain] table"
        raise ProverToolError(msg)
    channel = typ.cast("dict[str, object]", table).get("channel")
    if not isinstance(channel, str):
        msg = "upstream rust-toolchain.toml channel must be a string"
        raise ProverToolError(msg)
    if _CHANNEL.fullmatch(channel) is None:
        msg = f"upstream rust-toolchain.toml channel {channel!r} is not a dated nightly"
        raise ProverToolError(msg)
    return channel


def parse_crate_version(data: bytes) -> str:
    """Return the `kani-verifier` package version from `Cargo.toml`.

    Parameters
    ----------
    data : bytes
        Exact root `Cargo.toml` bytes.

    Returns
    -------
    str
        The package version, such as `0.68.0`.

    Raises
    ------
    ProverToolError
        Raised for invalid TOML, a missing `[package]` table or a non-string
        version.
    """
    document = _toml(data, "Cargo.toml")
    package = document.get("package")
    if not isinstance(package, dict):
        msg = "upstream Cargo.toml has no [package] table"
        raise ProverToolError(msg)
    version = typ.cast("dict[str, object]", package).get("version")
    if not isinstance(version, str) or not version.strip():
        msg = "upstream Cargo.toml package version must be a non-empty string"
        raise ProverToolError(msg)
    return version


def parse_dependency_versions(data: bytes) -> dict[str, str]:
    """Return the shell assignments in `kani-dependencies`.

    Parameters
    ----------
    data : bytes
        Exact `kani-dependencies` bytes.

    Returns
    -------
    dict[str, str]
        Assignment name to value, for example `CBMC_VERSION` to `6.11.0`.

    Raises
    ------
    ProverToolError
        Raised for a malformed assignment or a repeated name.
    """
    versions: dict[str, str] = {}
    for number, line in enumerate(_decode(data, "kani-dependencies").splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = _ASSIGNMENT.fullmatch(stripped)
        if match is None:
            msg = f'upstream kani-dependencies line {number} is not NAME="value"'
            raise ProverToolError(msg)
        name = match.group("name")
        if name in versions:
            msg = f"upstream kani-dependencies repeats {name}"
            raise ProverToolError(msg)
        versions[name] = match.group("value")
    return versions


def _path_assignment(line: str, number: int) -> str | None:
    """Return the path one `.gitmodules` assignment names, if it names one.

    Parameters
    ----------
    line : str
        Stripped, non-comment line.
    number : int
        One-based line number used in error messages.

    Returns
    -------
    str or None
        The path, or `None` when the assignment is a different key.

    Raises
    ------
    ProverToolError
        Raised for a line that is not a key=value pair or an empty path.
    """
    key, separator, value = line.partition("=")
    if not separator:
        msg = f"upstream .gitmodules line {number} is not a key=value pair"
        raise ProverToolError(msg)
    if key.strip() != "path":
        return None
    path = value.strip()
    if not path:
        msg = f"upstream .gitmodules path {path!r} is empty or repeated"
        raise ProverToolError(msg)
    return path


def _require_section_path(*, is_in_section: bool, has_path: bool) -> None:
    """Fail when the section that just ended declared no `path`.

    Parameters
    ----------
    is_in_section : bool
        Whether a section has been opened.
    has_path : bool
        Whether the open section declared a `path`.

    Raises
    ------
    ProverToolError
        Raised for a section with no `path` key.
    """
    if is_in_section and not has_path:
        msg = "upstream .gitmodules section has no path"
        raise ProverToolError(msg)


def parse_submodule_paths(data: bytes) -> tuple[str, ...]:
    """Return the submodule paths `.gitmodules` declares.

    Parameters
    ----------
    data : bytes
        Exact `.gitmodules` bytes.

    Returns
    -------
    tuple[str, ...]
        Paths in file order.

    Raises
    ------
    ProverToolError
        Raised for a malformed line, a section without a `path`, a `path`
        outside a section or a repeated path.
    """
    paths: list[str] = []
    in_section = False
    section_has_path = False
    for number, line in enumerate(_decode(data, ".gitmodules").splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", ";")):
            continue
        if _SECTION.fullmatch(stripped) is not None:
            _require_section_path(is_in_section=in_section, has_path=section_has_path)
            in_section = True
            section_has_path = False
            continue
        if not in_section:
            msg = f"upstream .gitmodules line {number} is outside a submodule section"
            raise ProverToolError(msg)
        path = _path_assignment(stripped, number)
        if path is None:
            continue
        if path in paths:
            msg = f"upstream .gitmodules path {path!r} is empty or repeated"
            raise ProverToolError(msg)
        paths.append(path)
        section_has_path = True
    _require_section_path(is_in_section=in_section, has_path=section_has_path)
    return tuple(paths)


def parse_licence_inventory(data: bytes) -> tuple[str, ...]:
    """Return the component names upstream's licence notes list.

    Parameters
    ----------
    data : bytes
        Exact `license-notes.txt` bytes.

    Returns
    -------
    tuple[str, ...]
        Component names in file order. Only unindented `name: URL` rows count;
        prose, acknowledgements and indented notes are ignored.
    """
    names = []
    for line in _decode(data, "license-notes.txt").splitlines():
        if line != line.lstrip():
            continue
        match = _NOTICE.fullmatch(line.rstrip())
        if match is not None:
            names.append(match.group("name"))
    return tuple(names)


def missing_licence_evidence(
    notes: bytes,
    licences: cabc.Sequence[LicenceRecord],
) -> tuple[str, ...]:
    """Return redistributed components whose licence evidence is missing.

    Parameters
    ----------
    notes : bytes
        Exact upstream `license-notes.txt` bytes.
    licences : collections.abc.Sequence of LicenceRecord
        Recorded components.

    Returns
    -------
    tuple[str, ...]
        Components that claim the upstream notice inventory without appearing
        in it, or that name no licence source at all. Sorted and unique.
    """
    listed = {name.casefold() for name in parse_licence_inventory(notes)}
    missing = {
        record.component
        for record in licences
        if (
            record.evidence == NOTICE_INVENTORY
            and record.component.casefold() not in listed
        )
        or not record.source.strip()
    }
    return tuple(sorted(missing))
