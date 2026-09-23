"""Contracts over the repository's GitHub Actions workflows.

The readers here are pure over supplied text or parsed documents, apart
from `reading.read_workflows`, which is the one filesystem boundary. That
split is what lets the rule tests ask what a rule makes of a workflow this
repository does not contain: the real files use one spelling of
everything, so they cannot tell a working reader from a broken one.
"""
