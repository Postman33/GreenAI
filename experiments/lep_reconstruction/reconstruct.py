"""Compatibility entry point for the production overhead-power reconstructor."""

from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from src.detection.overhead_power_reconstructor import *  # noqa: F401,F403,E402
from src.detection.overhead_power_reconstructor import main  # noqa: E402


if __name__ == "__main__":
    main()
