"""Editor-independent Hydra configuration intelligence.

All public positions and ranges use LSP's zero-based UTF-16 coordinates.
"""

from importlib.metadata import PackageNotFoundError, version

from hydradex.lsp_client import BackendError
from hydradex.service import HydraDex
from hydradex.settings import Settings

try:
    __version__ = version("hydradex")
except PackageNotFoundError:  # pragma: no cover - running from an uninstalled tree
    __version__ = "0+unknown"

__all__ = ["BackendError", "HydraDex", "Settings", "__version__"]
