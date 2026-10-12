"""Run the complete standard-library suite, failing when discovery is empty."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import TextIO

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def run_tests(start_dir: Path, *, stream: TextIO | None = None) -> int:
    stream = sys.stderr if stream is None else stream
    suite = unittest.TestLoader().discover(str(start_dir), pattern="test*.py")
    count = suite.countTestCases()
    if count == 0:
        print("error: no tests discovered", file=stream)
        return 2
    print(f"Discovered {count} tests on Python {sys.version.split()[0]} ({sys.platform})", file=stream)
    result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(run_tests(ROOT / "tests"))
