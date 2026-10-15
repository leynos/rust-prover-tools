"""Normalize inherited flags for Kani 0.67's literal-space input boundary.

The release workflow removes only measured development acceleration settings
and rejects ambiguous token encodings instead of weakening verification flags.
"""

from __future__ import annotations

import shlex
import typing as typ

from rust_prover_tools.errors import ProverToolError

if typ.TYPE_CHECKING:
    import collections.abc as cabc


def _option(tokens: tuple[str, ...], index: int) -> tuple[str, int]:
    """Read joined or paired compiler options without losing token boundaries."""
    token = tokens[index]
    if token not in {"-C", "-Z"}:
        return token, 1
    if index + 1 >= len(tokens) or tokens[index + 1].startswith("-"):
        msg = "Kani verification flags contain an incomplete -C/-Z option"
        raise ProverToolError(msg)
    return token + tokens[index + 1], 2


def _is_development_option(option: str) -> bool:
    """Identify only the approved development acceleration options."""
    return option.startswith("-Zthreads=") or option in {
        "-Clink-arg=-fuse-ld=mold",
        "-Zcodegen-backend=cranelift",
    }


def _validate_token(token: str) -> None:
    """Reject atoms that Kani's literal-space reader cannot round trip."""
    token.encode("utf-8", errors="strict")
    shlex.split(token)  # Validate quotes without stripping compiler cfg syntax.
    if not token or any(char.isspace() for char in token):
        msg = "Kani 0.67 cannot represent a whitespace-containing compiler flag"
        raise ProverToolError(msg)


def _effective_tokens(environment: cabc.Mapping[str, str]) -> tuple[str, ...]:
    """Decode effective inherited flags without changing Cargo precedence."""
    try:
        if "CARGO_ENCODED_RUSTFLAGS" in environment:
            encoded = environment["CARGO_ENCODED_RUSTFLAGS"]
            tokens = tuple(encoded.split("\x1f")) if encoded else ()
        else:
            tokens = tuple(environment.get("RUSTFLAGS", "").split())
        for token in tokens:
            _validate_token(token)
    except (ValueError, UnicodeError) as exc:
        msg = "Kani compiler flags must be valid UTF-8 with unambiguous tokens"
        raise ProverToolError(msg) from exc
    return tokens


def verification_flags(environment: cabc.Mapping[str, str]) -> tuple[str, ...]:
    """Normalize effective Cargo flags for the qualified legacy verifier.

    Parameters
    ----------
    environment : Mapping[str, str]
        Injected environment; a present encoded value wins, even when empty.

    Returns
    -------
    tuple[str, ...]
        Compatible flags in order, with development options removed and
        warning denial appended.

    Raises
    ------
    ProverToolError
        An independent backend, malformed option, or token unrepresentable by
        Kani's literal-space reader was supplied.
    """
    if any(
        key.startswith("CARGO_PROFILE_") and key.endswith("_CODEGEN_BACKEND")
        for key in environment
    ):
        msg = (
            "Kani exec cannot isolate a Cargo profile codegen backend; "
            "remove that setting"
        )
        raise ProverToolError(msg)
    tokens = _effective_tokens(environment)
    result: list[str] = []
    index = 0
    while index < len(tokens):
        option, width = _option(tokens, index)
        if "codegen-backend" in option and option != "-Zcodegen-backend=cranelift":
            msg = "Kani exec does not support a competing compiler backend"
            raise ProverToolError(msg)
        if not _is_development_option(option):
            result.extend(tokens[index : index + width])
        index += width
    return (
        (*result, "-D", "warnings")
        if tuple(result[-2:]) != ("-D", "warnings")
        else tuple(result)
    )
