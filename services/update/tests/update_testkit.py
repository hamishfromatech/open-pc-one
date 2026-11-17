"""Put ``src/`` on sys.path for the suite.

Not a conftest.py: pytest imports conftest modules by basename and this repository
already has one for provisioning, which collides (that is why the setup suite uses
a plain module too).
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))
