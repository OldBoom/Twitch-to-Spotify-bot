"""Resolve the writable data directory for config, tokens, and the blocklist.

When frozen (PyInstaller), files live next to the executable. When running from
source, they live in the project root (parent of ``src/``).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def data_dir() -> Path:
    if is_frozen():
        return Path(sys.executable).resolve().parent
    # src/app_paths.py -> project root
    return Path(__file__).resolve().parent.parent


def src_dir() -> Path | None:
    """Source tree for ``python -m`` launches; None when frozen."""
    if is_frozen():
        return None
    return Path(__file__).resolve().parent


def ensure_data_cwd() -> Path:
    """``chdir`` into the data directory so relative token/env paths resolve.

    Returns the data directory.
    """
    root = data_dir()
    os.chdir(root)
    return root
