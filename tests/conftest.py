"""
Standalone test configuration for hermes-chess plugin.

On macOS CairoSVG needs DYLD_LIBRARY_PATH set to find libcairo:
    DYLD_LIBRARY_PATH=/opt/homebrew/lib pytest tests/

On Linux (Fedora/dnf):
    sudo dnf install cairo cairo-devel python3-devel
    pytest tests/
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# Ensure src/ is on the path so `import hermes_chess` works
SRC_DIR = Path(__file__).resolve().parents[1] / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))


@pytest.fixture
def plugin():
    """Return the hermes_chess module for introspection."""
    import hermes_chess
    return hermes_chess
