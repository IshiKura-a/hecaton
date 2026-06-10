"""Hecaton bootstrap helpers — Python sibling of the lib/*.sh modules.

The .cache/node-name/<host> files are shared with lib/remote.sh, so a
host probed by either side is visible to the other.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import NoReturn


def hecaton_root() -> Path:
    """Repo root. Set by the bash wrapper via $HECATON_ROOT."""
    v = os.environ.get("HECATON_ROOT")
    if not v:
        die("$HECATON_ROOT not set (run via the .sh wrapper)")
    return Path(v)


def log(msg: str) -> None:
    print(f"[hecaton] {msg}", file=sys.stderr, flush=True)


def warn(msg: str) -> None:
    print(f"[hecaton] WARN: {msg}", file=sys.stderr, flush=True)


def die(msg: str) -> NoReturn:
    print(f"[hecaton] ERROR: {msg}", file=sys.stderr, flush=True)
    sys.exit(1)
