"""Read pinned versions from lib/*-version.sh.

The .sh files remain the single source of truth so bash phases can
keep sourcing them. Python phases parse out `KEY="value"` lines.
"""

from __future__ import annotations

import re

from . import die, hecaton_root

_LINE = re.compile(r'^\s*([A-Z_][A-Z0-9_]*)="([^"]*)"\s*(?:#.*)?$')


def load(filename: str) -> dict[str, str]:
    """Parse KEY=\"value\" assignments out of lib/<filename>."""
    f = hecaton_root() / "lib" / filename
    if not f.is_file():
        die(f"missing version file: {f}")
    out: dict[str, str] = {}
    for line in f.read_text().splitlines():
        m = _LINE.match(line)
        if m:
            out[m.group(1)] = m.group(2)
    return out
