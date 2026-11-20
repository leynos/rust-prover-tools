"""Hold the error the trust audit raises for source it refuses to read.

The error lives apart from the lexer so the literal decoder can raise it
without importing the module that tokenizes literals. The dependency runs one
way — lexer and decoder both depend on this module — where a single exception
shared through the lexer would make the two import each other.
"""

from __future__ import annotations

__all__ = ("TrustAuditError",)


class TrustAuditError(Exception):
    """Report source the audit refuses to read."""
