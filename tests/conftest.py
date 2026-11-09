"""pytest configuration for lean-mode tests."""
import sys
from pathlib import Path

# Ensure the plugin directory is on sys.path for direct imports
_here = Path(__file__).parent  # tests/
_root = _here.parent           # plugin root
for p in [_root, _here]:
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))
