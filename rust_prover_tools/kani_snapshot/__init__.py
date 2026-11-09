"""Validated, immutable Kani snapshot distribution support.

The package is split by concern:

- `models` holds the frozen validated records; `cache_key` derives their
  deterministic cache key.
- `pins`, `manifest`, `manifest_groups`, `tags`, `strict_json` and `validation`
  parse untrusted bytes into those records; `render` reproduces manifest bytes.
- `integrity` verifies manifests, assets and build attestations.
- `download`, `archive`, `install`, `identity` and `run` deliver the consumer
  feature.
- `build`, `resolution` and `publish` are producer-side automation.

Consumer commands never compile Kani or a solver, and never fall back to the
legacy release installer or a source build.
"""

from __future__ import annotations

__all__: tuple[str, ...] = ()
