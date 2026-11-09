"""Repository and internal authoring tooling (never included in learner bundles)."""

import sys
from pathlib import Path

REPO_ROOT = (
    Path(getattr(sys, "_MEIPASS", "")) / "authoring-data"
    if getattr(sys, "frozen", False)
    else Path(__file__).resolve().parents[2]
)
