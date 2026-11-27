"""Run one real-process witness case in this isolated interpreter.

The witness tests start this script with ``python -I`` so a stuck settlement
cannot block the pytest process or its shutdown.
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import tests.supervision_witness_children_support  # ruff: ignore[unused-import, module-import-not-at-top-of-file]
from tests.supervision_witness_cases_support import main  # ruff: ignore[module-import-not-at-top-of-file]

if __name__ == "__main__":
    main(sys.argv[1], Path(sys.argv[2]))
