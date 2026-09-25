"""Hydra's config-directory layout and package placement rules, in one place.

The placement rules mirror Hydra 1.3, and are checked against real Hydra
composition in tests/test_overrides.py:

- A config's package defaults to its parent's package followed by the group key
  as written in the defaults list, so `server@web: apache` places the nested
  `db: mysql` at `web.db`. Absolute groups like `/db` only affect file lookup.
- A defaults-list `@package` override is relative to the parent's package, with
  `_here_` meaning the parent package and `_group_` meaning the group key.
- A `# @package` header is absolute. Only `_global_` is special in headers.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

CONVENTIONAL_ROOTS = frozenset({"conf", "config", "configs"})
Package = tuple[str, ...]


def config_root(path: Path, workspace_root: Path, configured: Sequence[Path]) -> Path | None:
    """The config directory containing `path`: the deepest configured root, else the
    nearest conventional `conf`/`config`/`configs` ancestor inside the workspace."""
    containing = [root for root in configured if path.is_relative_to(root)]
    if containing:
        return max(containing, key=lambda root: len(root.parts))
    return next(
        (
            parent
            for parent in path.parents
            if parent.name in CONVENTIONAL_ROOTS and parent.is_relative_to(workspace_root)
        ),
        None,
    )


def group(path: Path, base: Path) -> Package:
    """The config group of a file, relative to its config directory."""
    return path.parent.relative_to(base).parts if path.is_relative_to(base) else ()


def header_package(name: str) -> Package:
    """An absolute `# @package` header value."""
    if name in ("", "_global_"):
        return ()
    return tuple(name.removeprefix("_global_.").split("."))


def override_package(name: str, *, key: Package, parent: Package) -> Package:
    """A defaults-list `group@package` override, relative to the parent's package."""
    if name == "_global_":
        return ()
    if name.startswith("_global_."):
        return _expand(name.removeprefix("_global_."), key)
    if name == "_here_":
        return parent
    return (*parent, *_expand(name, key))


def child_package(
    override: str | None, header: str | None, *, key: Package, parent: Package
) -> Package:
    """Where a defaults-list reference is placed. Overrides take precedence over headers."""
    if override is not None:
        return override_package(override, key=key, parent=parent)
    if header is not None:
        return header_package(header)
    return (*parent, *key)


def _expand(name: str, key: Package) -> Package:
    parts: list[str] = []
    for part in name.split("."):
        parts.extend(key if part == "_group_" else (part,))
    return tuple(parts)
