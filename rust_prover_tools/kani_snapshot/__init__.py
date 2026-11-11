"""Validated, immutable Kani snapshot distribution support.

The package is split by concern:

- `models` holds the frozen validated records; `cache_key` derives their
  deterministic cache key.
- `pins`, `manifest`, `manifest_groups`, `tags`, `strict_json`, `policy` and
  `validation` parse untrusted bytes into those records; `render` reproduces
  manifest bytes.
- `integrity` verifies manifests, assets and build attestations.
- The consumer feature is implemented by `download`, `archive`, `install`,
  `identity` and `run`, with `attestation`, `publication` and `activation`
  supplying the authenticated publication protocol those commands rely on.
- Later milestones add the producer automation (`build`, `resolution`,
  `publish`); those modules do not exist yet.

Consumer commands never compile Kani or a solver, and never fall back to the
legacy release installer or a source build. Attestation is verified by a
separate GitHub CLI process rather than by code in this package.
"""

from __future__ import annotations

__all__: tuple[str, ...] = ()
