"""The `gh` adapter that implements the release port.

This package is the only half of publication that touches the outside world,
and it is split by the kind of outbound call each step makes:

- `api` runs `gh` commands and drives the publication state machine.
- `document` turns one API response into an observed state.
- `download` fetches and authenticates the bytes a release serves.
- `attestation` decides whether a served attestation bundle authenticates a
  manifest, structurally and then by signature.

The split follows effects rather than data shape: `document` and most of
`attestation` parse what was read, while `api` and `download` are the modules
that start a process or perform a transfer. Nothing in the package imports
another adapter, so each seam can be changed without reaching across into the
next.
"""

from __future__ import annotations

__all__: tuple[str, ...] = ()
