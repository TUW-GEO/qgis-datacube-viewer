from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# scripts/ is not an importable package; make its modules available to tests.
sys.path.insert(0, str(REPO_ROOT / "scripts"))
