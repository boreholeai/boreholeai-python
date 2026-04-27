"""Shared pytest setup: put `src/` on sys.path so tests can import boreholeai."""

import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "src"))
