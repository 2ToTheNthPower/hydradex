"""Validated library and LSP configuration."""

from __future__ import annotations

from dataclasses import dataclass, fields, replace
from math import isfinite
from pathlib import Path
from typing import Any, Literal

MatchFilter = Literal["all", "top matches only", "perfect matches only"]
MATCH_FILTERS = ("all", "top matches only", "perfect matches only")

LSP_NAMES = {
    "excludePatterns": "exclude_patterns",
    "matchFilter": "match_filter",
    "isolateWorkspaceFolders": "isolate_workspace_folders",
    "pythonPath": "python_path",
    "extraPaths": "extra_paths",
    "configRoots": "config_roots",
    "backendCommand": "backend_command",
    "backendTimeout": "backend_timeout",
}
_LISTS = ("exclude_patterns", "extra_paths", "config_roots", "backend_command")


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
        lsp_name = {v: k for k, v in LSP_NAMES.items()}
        for name in _LISTS:
            value = getattr(self, name)
            if not isinstance(value, (list, tuple)) or not all(isinstance(x, str) for x in value):
                raise ValueError(f"{lsp_name[name]} must be a list of strings")
            object.__setattr__(self, name, tuple(value))
        if self.match_filter not in MATCH_FILTERS:
            raise ValueError(
                f"Unknown matchFilter {self.match_filter!r}; expected one of {MATCH_FILTERS}"
            )
        timeout = self.backend_timeout
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not isfinite(timeout)
            or timeout <= 0
        ):
            raise ValueError("backendTimeout must be a positive number of seconds")
        if not isinstance(self.isolate_workspace_folders, bool):
            raise ValueError("isolateWorkspaceFolders must be a boolean")
        if self.python_path is not None and not isinstance(self.python_path, str):
            raise ValueError("pythonPath must be a string")

    def merge(self, data: dict[str, Any]) -> tuple[Settings, list[str]]:
        """Apply camelCase LSP keys present in `data`; return unknown keys as warnings.

        Keys absent from `data` keep their current values. An explicit null restores
        the default for that key.
        """
        if not isinstance(data, dict):
            raise ValueError("HydraDex settings must be an object")
        defaults = {f.name: f.default for f in fields(Settings)}
        changes = {}
        for key, value in data.items():
            if key in LSP_NAMES:
                name = LSP_NAMES[key]
                changes[name] = defaults[name] if value is None else value
        unknown = [f"Unknown HydraDex setting {key!r}" for key in data if key not in LSP_NAMES]
        return replace(self, **changes), unknown

    @classmethod
    def from_lsp(cls, value: Any) -> tuple[Settings, list[str]]:
        """Parse initialization options, given flat or under a `hydradex` key."""
        if value is None:
            return cls(), []
        if not isinstance(value, dict):
            raise ValueError("HydraDex settings must be an object")
        section = value.get("hydradex", value)
        return cls().merge(section if section is not None else {})

    def paths(self, values: tuple[str, ...], root: Path) -> list[Path]:
        return [(root / Path(p).expanduser()).resolve() for p in values]


def settings_section(value: Any) -> dict[str, Any] | None:
    """The `hydradex` section of a didChangeConfiguration payload, if it has one."""
    if isinstance(value, dict) and isinstance(value.get("hydradex"), dict):
        return value["hydradex"]
    return None
