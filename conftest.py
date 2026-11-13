"""Pytest root configuration.

Ensures the repository root and src/ are importable so tests can load both
the ``hermes_payments`` package and the in-repo plugin package.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent

for _p in (ROOT, ROOT / "src"):
    _p_str = str(_p)
    if _p_str not in sys.path:
        sys.path.insert(0, _p_str)
