"""Test package bootstrap.

Puts src/ on sys.path so `python -m unittest` (or `python -m unittest
discover`) works from the repo root with no install and no PYTHONPATH
needed — matches the project's stdlib-only, no-pip-dependencies rule.
"""

import os
import sys

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)
