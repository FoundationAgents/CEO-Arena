"""Keep Harness-local factories importable without changing project packaging."""

import sys
from pathlib import Path

HARNESS_TESTS = str(Path(__file__).resolve().parent)
if HARNESS_TESTS not in sys.path:
    sys.path.insert(0, HARNESS_TESTS)
