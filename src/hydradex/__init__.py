"""Editor-independent Hydra configuration intelligence.

All public positions and ranges use LSP's zero-based UTF-16 coordinates.
"""

from hydradex.service import HydraDex
from hydradex.settings import Settings

__all__ = ["HydraDex", "Settings"]
__version__ = "0.2.0"
