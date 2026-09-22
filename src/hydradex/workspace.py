"""Workspace indexing, open-buffer overlays, and Hydra config discovery."""

from __future__ import annotations

import logging
import os
import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

import pathspec
from lsprotocol import types as lsp

from hydradex.document import Default, Document, Entry, uri_path
from hydradex.settings import Settings

log = logging.getLogger(__name__)


@dataclass
class Definition:
    path: tuple[str, ...]
    location: lsp.Location


class Workspace:
    def __init__(self, roots: list[Path], settings: Settings):
        self.roots = sorted({p.resolve() for p in roots}, key=lambda p: -len(p.parts))
        self.settings = settings
        self.excludes = pathspec.PathSpec.from_lines("gitwildmatch", settings.exclude_patterns)
        self.documents: dict[str, Document] = {}
        self.open_uris: set[str] = set()
        self.index: dict[tuple[str, ...], list[Definition]] = defaultdict(list)
        self.file_keys: dict[str, set[tuple[str, ...]]] = {}

    def root_for(self, path: Path) -> Path | None:
        return next((root for root in self.roots if path.is_relative_to(root)), None)

    def included(self, path: Path) -> bool:
        root = self.root_for(path)
        return root is not None and not self.excludes.match_file(path.relative_to(root).as_posix())

    def refresh(self) -> None:
        overlays = [self.documents[uri] for uri in self.open_uris]
        self.documents.clear()
        self.index.clear()
        self.file_keys.clear()
        for root in self.roots:
            for parent, dirs, files in os.walk(root, followlinks=False):
                dirs[:] = sorted(
                    d
                    for d in dirs
                    if not self.excludes.match_file(
                        (Path(parent) / d).relative_to(root).as_posix() + "/"
                    )
                    and not (Path(parent) / d).is_symlink()
                )
                for name in sorted(files):
                    path = Path(parent) / name
                    if path.suffix.lower() in (".yaml", ".yml") and not path.is_symlink():
                        self.load(path)
        for document in overlays:
            self.put(document)

    def load(self, path: Path) -> None:
        if not self.included(path):
            return
        try:
            self.put(Document(path.as_uri(), path.read_text(encoding="utf-8")))
        except (OSError, UnicodeError) as exc:
            log.debug("Cannot index %s: %s", path, exc)
            self.remove(path.as_uri())

    def package(self, document: Document, path: Path, root: Path) -> tuple[str, ...]:
        match = re.search(r"^\s*#\s*@package\s+(\S+)", document.text, re.MULTILINE)
        if match:
            package = match[1]
            if package == "_global_":
                return ()
            if package != "_group_":
                return tuple(package.split("."))
        return path.parent.relative_to(root).parts

    def put(self, document: Document, *, opened: bool = False) -> None:
        self.remove(document.uri)
        self.documents[document.uri] = document
        if opened:
            self.open_uris.add(document.uri)
        path = uri_path(document.uri)
        root = self.root_for(path) if path else None
        if path is None or root is None or not self.included(path):
            return
        package = self.package(document, path, root)
        keys: set[tuple[str, ...]] = set()
        for entry in document.entries:
            if entry.path[0] == "defaults":
                continue
            logical = (*package, *entry.path)
            definition = Definition(
                logical, lsp.Location(document.uri, document.node_range(entry.key))
            )
            for i in range(len(logical)):
                suffix = logical[i:]
                self.index[suffix].append(definition)
                keys.add(suffix)
        self.file_keys[document.uri] = keys

    def remove(self, uri: str) -> None:
        self.documents.pop(uri, None)
        for key in self.file_keys.pop(uri, set()):
            remaining = [d for d in self.index[key] if d.location.uri != uri]
            if remaining:
                self.index[key] = remaining
            else:
                del self.index[key]

    def close(self, uri: str) -> None:
        self.open_uris.discard(uri)
        self.remove(uri)
        path = uri_path(uri)
        if path:
            self.load(path)

    def changed(self, uri: str, *, deleted: bool = False) -> None:
        if uri in self.open_uris:
            return
        self.remove(uri)
        path = uri_path(uri)
        if path and not deleted and path.suffix.lower() in (".yaml", ".yml"):
            self.load(path)

    def resolve(self, expression: str, document: Document, entry: Entry) -> list[lsp.Location]:
        if expression.startswith("."):
            levels = len(expression) - len(expression.lstrip("."))
            parent = entry.path[:-1]
            if levels > len(parent) + 1:
                return []
            target = (*parent[: len(parent) - levels + 1], *expression[levels:].split("."))
            return [
                lsp.Location(document.uri, document.node_range(e.key))
                for e in document.entries
                if e.path == target
            ]
        parts = tuple(expression.split("."))
        source = uri_path(document.uri)
        root = self.root_for(source) if source else None
        matches: list[tuple[int, lsp.Location]] = []
        seen: set[tuple[str, int, int]] = set()
        for level in range(len(parts), 0, -1):
            for definition in self.index.get(parts[-level:], []):
                location = definition.location
                path = uri_path(location.uri)
                if self.settings.isolate_workspace_folders and (
                    path is None or self.root_for(path) != root
                ):
                    continue
                key = (location.uri, location.range.start.line, location.range.start.character)
                if key not in seen:
                    seen.add(key)
                    matches.append((level, location))
        matches.sort(
            key=lambda x: (-x[0], x[1].uri, x[1].range.start.line, x[1].range.start.character)
        )
        if not matches:
            return []
        threshold = {
            "all": 0,
            "top matches only": matches[0][0],
            "perfect matches only": len(parts),
        }[self.settings.match_filter]
        return [location for level, location in matches if level >= threshold]

    def default_paths(self, document: Document, default: Default) -> list[Path]:
        source = uri_path(document.uri)
        root = self.root_for(source) if source else None
        if source is None or root is None or "${" in default.name:
            return []
        name = default.name.lstrip("/")
        if Path(name).suffix.lower() in (".yaml", ".yml"):
            name = str(Path(name).with_suffix(""))
        configured = self.settings.paths(self.settings.config_roots, root)
        ancestors = [source.parent, *source.parent.parents]
        ancestors = [p for p in ancestors if p.is_relative_to(root)]
        if default.name.startswith("/"):
            # Conventional config directories are useful without explicit configuration.
            conventional = [p for p in ancestors if p.name in ("conf", "config", "configs")]
            bases = configured or conventional or [root]
        else:
            bases = [*ancestors, *configured]
        result = []
        for base in bases:
            for suffix in (".yaml", ".yml"):
                path = (base / (name + suffix)).resolve()
                if path.is_relative_to(root) or any(path.is_relative_to(p) for p in configured):
                    result.append(path)
        return list(dict.fromkeys(result))

    def default_location(self, document: Document, default: Default) -> lsp.Location | None:
        for path in self.default_paths(document, default):
            if path.as_uri() in self.documents or path.is_file():
                return lsp.Location(
                    path.as_uri(), lsp.Range(lsp.Position(0, 0), lsp.Position(0, 0))
                )
        return None
