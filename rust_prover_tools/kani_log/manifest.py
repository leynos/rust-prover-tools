"""Hold the harness manifest a Kani analysis is taken against.

A log says what happened; it cannot say what was meant to happen.  The manifest
supplies the missing half: which harnesses are claims and which are negative
controls, which covers each claim declares, and which property each control is
supposed to fail.  A manifest-bound claim set is also what turns a harness
missing from the log into ``skipped`` rather than into silence.

The document is parsed with the standard-library JSON module.  Duplicate keys
and non-finite constants are refused rather than silently resolved, because a
manifest is a user-written input and a parser that guesses is worse than one
that names the problem.
"""

from __future__ import annotations

import collections.abc as cabc
import dataclasses as dc
import json
import typing as typ

from rust_prover_tools.errors import ProverToolError

__all__ = (
    "CLAIM",
    "CONTROL",
    "MANIFEST_SCHEMA_VERSION",
    "ROLES",
    "Harness",
    "KaniManifestError",
    "Manifest",
    "manifest_from_document",
    "parse_manifest",
)

CLAIM = "claim"
CONTROL = "control"
ROLES = frozenset({CLAIM, CONTROL})
MANIFEST_SCHEMA_VERSION = 1
_MANIFEST_KEYS = frozenset({"schema_version", "harnesses"})
_HARNESS_KEYS = frozenset(
    {"name", "role", "covers", "expected_failure", "should_panic"},
)


class KaniManifestError(ProverToolError):
    """A harness manifest that cannot be read as a declaration of expectations."""


def _fail(message: str) -> typ.NoReturn:
    """Raise one manifest error with the required context prefix."""
    msg = f"Kani harness manifest {message}"
    raise KaniManifestError(msg)


@dc.dataclass(frozen=True, slots=True)
class Harness:
    """One manifest entry declaring a claim or a negative control.

    A control must name the check it is expected to fail and may not be a
    ``should_panic`` harness: that flag belongs to a claim whose failure is the
    panic Kani was asked to expect.  Refusing both at construction keeps them
    out of the analysis rather than making every log pay to re-check them.
    """

    name: str
    role: str
    covers: tuple[str, ...] = ()
    expected_failure: str | None = None
    should_panic: bool = False

    def __post_init__(self) -> None:
        """Refuse an entry the analysis could only guess at."""
        self._require_field_types()
        self._require_role_rules()

    def _require_field_types(self) -> None:
        """Refuse a field whose type cannot describe a declaration."""
        if not isinstance(self.name, str) or not self.name:
            _fail("entry does not name a harness")
        if not isinstance(self.role, str) or self.role not in ROLES:
            _fail(f"harness {self.name!r} has unknown role {self.role!r}")
        if not isinstance(self.covers, tuple) or not all(
            isinstance(cover, str) for cover in self.covers
        ):
            _fail(f"harness {self.name!r} has a malformed covers list")
        if self.expected_failure is not None and not isinstance(
            self.expected_failure,
            str,
        ):
            _fail(f"harness {self.name!r} has a malformed expected_failure")
        if not isinstance(self.should_panic, bool):
            _fail(f"harness {self.name!r} has a malformed should_panic")

    def _require_role_rules(self) -> None:
        """Refuse a role combined with expectations that cannot hold."""
        if self.role == CONTROL and self.should_panic:
            _fail(
                f"control {self.name!r} declares should_panic, which may mark "
                "only a claim",
            )
        if self.role == CONTROL and not self.expected_failure:
            _fail(
                f"control {self.name!r} names no property it is expected to fail",
            )
        unreachable_covers = bool(self.covers) and self.should_panic
        if self.role == CLAIM and unreachable_covers:
            _fail(
                f"claim {self.name!r} is should_panic, so its covers would be "
                "unreachable",
            )


@dc.dataclass(frozen=True, slots=True)
class Manifest:
    """Declared roles, covers and expected control failures for one run."""

    harnesses: tuple[Harness, ...]

    def __post_init__(self) -> None:
        """Refuse an ambiguous manifest."""
        names = self.names()
        if len(set(names)) != len(names):
            _fail("declares a harness twice")

    @property
    def claims(self) -> tuple[Harness, ...]:
        """The harnesses whose success an analysis may rest on."""
        return tuple(item for item in self.harnesses if item.role == CLAIM)

    @property
    def controls(self) -> tuple[Harness, ...]:
        """The negative controls, which must fail for the declared reason."""
        return tuple(item for item in self.harnesses if item.role == CONTROL)

    def names(self) -> tuple[str, ...]:
        """Return every declared harness name in manifest order."""
        return tuple(item.name for item in self.harnesses)

    def declared_covers(self) -> tuple[str, ...]:
        """Return every cover description a claim declares, in manifest order."""
        return tuple(cover for item in self.claims for cover in item.covers)

    def select(self, names: tuple[str, ...]) -> Manifest:
        """Narrow the manifest to the harnesses one log claims.

        Parameters
        ----------
        names : tuple[str, ...]
            Harness names to keep, in the order requested.

        Returns
        -------
        Manifest
            The narrowed manifest.

        Raises
        ------
        KaniManifestError
            If a name is not declared or is named twice.

        """
        by_name = {item.name: item for item in self.harnesses}
        unknown = [name for name in names if name not in by_name]
        if unknown:
            _fail(f"does not declare {unknown}")
        if len(set(names)) != len(names):
            _fail(f"selection names a harness twice: {list(names)}")
        return Manifest(tuple(by_name[name] for name in names))


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Build a JSON object, refusing any duplicated key."""
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            _fail(f"repeats the key {key!r}")
        result[key] = value
    return result


def _reject_constant(value: str) -> typ.NoReturn:
    """Refuse a non-finite JSON constant such as ``NaN`` or ``Infinity``."""
    _fail(f"contains the non-finite constant {value}")


def parse_manifest(text: str) -> Manifest:
    """Read a harness manifest from JSON text.

    Parameters
    ----------
    text : str
        Manifest document text.

    Returns
    -------
    Manifest
        The validated manifest.

    Raises
    ------
    KaniManifestError
        If the text is not valid JSON, repeats a key, contains a non-finite
        constant, nests deeper than the decoder can read, or describes an
        invalid manifest.

    """
    try:
        document = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except json.JSONDecodeError as error:
        _fail(f"is not valid JSON: {error.msg} at line {error.lineno}")
    except ValueError as error:
        # A digit run past CPython's integer-string limit fails here rather
        # than as a syntax error, and must still leave as a manifest error.
        _fail(f"contains a number this parser cannot read: {error}")
    except RecursionError:
        # JSON has no depth limit, but the decoder's recursion does.  The
        # document is invalid input, not a fault in this program, so it leaves
        # the same way every other unreadable manifest does.
        _fail("is nested too deeply for this parser to read")
    if not isinstance(document, cabc.Mapping):
        _fail("must be a JSON object")
    return manifest_from_document(document)


def manifest_from_document(document: cabc.Mapping[str, object]) -> Manifest:
    """Build and validate a manifest from a decoded JSON object.

    Parameters
    ----------
    document : collections.abc.Mapping[str, object]
        Decoded manifest mapping.

    Returns
    -------
    Manifest
        The validated manifest.

    Raises
    ------
    KaniManifestError
        If a key, value or role violates the manifest contract.

    """
    unknown = set(document) - _MANIFEST_KEYS
    if unknown:
        _fail(f"has unknown keys {sorted(unknown)}")
    version = document.get("schema_version")
    if isinstance(version, bool) or version != MANIFEST_SCHEMA_VERSION:
        _fail(f"must declare schema_version {MANIFEST_SCHEMA_VERSION}")
    entries = document.get("harnesses")
    if not isinstance(entries, list):
        _fail("has no harnesses array")
    if not entries:
        _fail("declares no harnesses")
    harnesses = tuple(_harness_from_entry(entry) for entry in entries)
    return Manifest(harnesses)


def _harness_from_entry(entry: object) -> Harness:
    """Validate one harness entry and convert it to a record."""
    if not isinstance(entry, cabc.Mapping):
        _fail(f"entry {entry!r} is not an object")
    unknown = set(entry) - _HARNESS_KEYS
    if unknown:
        _fail(f"entry {entry.get('name')!r} has unknown keys {sorted(unknown)}")
    name = entry.get("name")
    role = entry.get("role")
    if not isinstance(name, str) or not isinstance(role, str):
        _fail(f"entry {entry!r} does not name a harness and a role")
    covers = entry.get("covers", [])
    if not isinstance(covers, list) or not all(
        isinstance(cover, str) for cover in covers
    ):
        _fail(f"harness {name!r} has a malformed covers list")
    expected = entry.get("expected_failure")
    if expected is not None and not isinstance(expected, str):
        _fail(f"harness {name!r} has a malformed expected_failure")
    # `should_panic` is what lets a failed claim be excused, so a truthful type
    # matters: `bool("false")` would accept the string as a panic claim.
    should_panic = entry.get("should_panic", False)
    if not isinstance(should_panic, bool):
        _fail(f"harness {name!r} has a malformed should_panic")
    return Harness(name, role, tuple(covers), expected, should_panic)
