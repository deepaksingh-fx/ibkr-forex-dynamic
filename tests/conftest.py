"""
Pytest setup. Adds the project root to sys.path so tests can import top-level
modules (time_utils, cpr, ...) directly.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
