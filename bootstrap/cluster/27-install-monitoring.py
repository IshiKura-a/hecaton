"""Phase 27 entrypoint for monitoring.

Implementation lives in bootstrap/lib/monitoring.py so the phase script stays
as orchestration glue only.
"""

from __future__ import annotations

import sys
from pathlib import Path

# bootstrap/lib lives next to bootstrap/cluster.
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from lib.monitoring import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
