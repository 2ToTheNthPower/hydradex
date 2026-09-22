"""Validated library and LSP configuration."""

from dataclasses import dataclass
from math import isfinite
from pathlib import Path
from typing import Any, Literal

MatchFilter = Literal["all", "top matches only", "perfect matches only"]


@dataclass(frozen=True)
class Settings:
    exclude_patterns: tuple[str, ...] = (
        ".git/",
        ".venv/",
        "venv/",
        "node_modules/",
        "__pycache__/",
        "dist/",
        "build/",
    )
    match_filter: MatchFilter = "top matches only"
    isolate_workspace_folders: bool = True
    python_path: str | None = None
    extra_paths: tuple[str, ...] = ()
    config_roots: tuple[str, ...] = ()
    backend_command: tuple[str, ...] = ()
    backend_timeout: float = 15.0

    def __post_init__(self) -> None:
        if self.match_filter not in ("all", "top matches only", "perfect matches only"):
            raise ValueError(f"Unknown matchFilter: {self.match_filter}")
        if (
            not isinstance(self.backend_timeout, (int, float))
            or not isfinite(self.backend_timeout)
            or self.backend_timeout <= 0
        ):
            raise ValueError("backendTimeout must be a positive number")
        for name in ("exclude_patterns", "extra_paths", "config_roots", "backend_command"):
            value = getattr(self, name)
            if not isinstance(value, (list, tuple)) or not all(isinstance(x, str) for x in value):
                raise ValueError(f"{name} must be a list of strings")
        if not isinstance(self.isolate_workspace_folders, bool):
            raise ValueError("isolateWorkspaceFolders must be a boolean")
        if self.python_path is not None and not isinstance(self.python_path, str):
            raise ValueError("pythonPath must be a string")

    @classmethod
    def from_lsp(cls, value: dict[str, Any] | None) -> "Settings":
        if value is not None and not isinstance(value, dict):
            raise ValueError("HydraDex settings must be an object")
        data = (value or {}).get("hydradex", value or {})
        if not isinstance(data, dict):
            raise ValueError("HydraDex settings must be an object")
        names = {
            "excludePatterns": "exclude_patterns",
            "matchFilter": "match_filter",
            "isolateWorkspaceFolders": "isolate_workspace_folders",
            "pythonPath": "python_path",
            "extraPaths": "extra_paths",
            "configRoots": "config_roots",
            "backendCommand": "backend_command",
            "backendTimeout": "backend_timeout",
        }
        return cls(**{names[k]: v for k, v in data.items() if k in names})

    def paths(self, values: tuple[str, ...], root: Path) -> list[Path]:
        return [(root / Path(p).expanduser()).resolve() for p in values]
