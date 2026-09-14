"""Test collection helpers.

Loads the in-repo Hermes Payments plugin (directory ``plugins/hermes-payments``)
under a valid Python name so tests can import it despite the hyphen in the
plugin's on-disk name (required by Hermes plugin discovery).
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent / "plugins"
PLUGIN_INIT = PLUGIN_ROOT / "hermes-payments" / "__init__.py"

if PLUGIN_INIT.exists() and "hermes_payments_plugin" not in sys.modules:
    spec = importlib.util.spec_from_file_location(
        "hermes_payments_plugin", PLUGIN_INIT
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["hermes_payments_plugin"] = module
    spec.loader.exec_module(module)
