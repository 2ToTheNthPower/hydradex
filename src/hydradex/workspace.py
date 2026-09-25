"""Workspace YAML indexing, open-buffer overlays, and config file resolution."""

from __future__ import annotations

import logging
import os
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

import pathspec
from lsprotocol import types as lsp

from hydradex import hydra
from hydradex.document import Default, Document, Entry, uri_path
from hydradex.settings import Settings

log = logging.getLogger(__name__)

YAML_SUFFIXES = (".yaml", ".yml")


@dataclass
class Definition:
    path: tuple[str, ...]
    location: lsp.Location


class Workspace:
    def __init__(self, roots: list[Path], settings: Settings):
        # Deepest first, so nested workspace folders own their files.
        self.roots = sorted({p.resolve() for p in roots}, key=lambda p: -len(p.parts))
        self.settings = settings
        self.excludes = pathspec.PathSpec.from_lines("gitwildmatch", settings.exclude_patterns)
        self.documents: dict[str, Document] = {}
        self.open_uris: set[str] = set()
        self.index: dict[tuple[str, ...], list[Definition]] = defaultdict(list)
        self._file_keys: dict[str, set[tuple[str, ...]]] = {}

    def root_for(self, path: Path) -> Path | None:
        return next((root for root in self.roots if path.is_relative_to(root)), None)

    def included(self, path: Path) -> bool:
        root = self.root_for(path)
        return root is not None and not self.excludes.match_file(path.relative_to(root).as_posix())

    def config_roots(self, root: Path) -> list[Path]:
        return self.settings.paths(self.settings.config_roots, root)

    def config_root(self, path: Path) -> Path | None:
        root = self.root_for(path)
        return hydra.config_root(path, root, self.config_roots(root)) if root else None

    # Indexing

    def refresh(self) -> None:
        overlays = [self.documents[uri] for uri in self.open_uris if uri in self.documents]
        self.documents.clear()
        self.index.clear()
        self._file_keys.clear()
        for root in self.roots:
            for parent, dirs, files in os.walk(root, followlinks=False):
                base = Path(parent)
                dirs[:] = sorted(
                    d
                    for d in dirs
                    if not (base / d).is_symlink()
                    and not self.excludes.match_file((base / d).relative_to(root).as_posix() + "/")
                )
                for name in sorted(files):
                    path = base / name
                    if path.suffix.lower() in YAML_SUFFIXES and not path.is_symlink():
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

    def put(self, document: Document, *, opened: bool = False) -> None:
        self.remove(document.uri)
        self.documents[document.uri] = document
        if opened:
            self.open_uris.add(document.uri)
        path = uri_path(document.uri)
        root = self.root_for(path) if path else None
        if path is None or root is None or not self.included(path):
            return
        package = self.package(document, path)
        keys: set[tuple[str, ...]] = set()
        for entry in document.entries:
            if entry.path[0] == "defaults":
                continue
            logical = (*package, *entry.path)
            definition = Definition(
                logical, lsp.Location(document.uri, document.node_range(entry.key))
            )
            # Index every suffix so partial interpolation paths can be ranked.
            for i in range(len(logical)):
                self.index[logical[i:]].append(definition)
                keys.add(logical[i:])
        self._file_keys[document.uri] = keys

    def package(self, document: Document, path: Path) -> hydra.Package:
        """Where a file's keys land when selected from the primary config's defaults.

        This is an estimate: the real package also depends on the referencing chain,
        which is why interpolation lookup ranks partial path matches.
        """
        header = document.header.get("package")
        if header is not None:
            return hydra.header_package(header)
        base = self.config_root(path) or self.root_for(path)
        return hydra.group(path, base) if base else ()

    def remove(self, uri: str) -> None:
        self.documents.pop(uri, None)
        for key in self._file_keys.pop(uri, set()):
            remaining = [d for d in self.index[key] if d.location.uri != uri]
            if remaining:
                self.index[key] = remaining
            else:
                del self.index[key]

    def close(self, uri: str) -> None:
        self.open_uris.discard(uri)
        self.remove(uri)
        if path := uri_path(uri):
            self.load(path)

    def changed(self, uri: str, *, deleted: bool = False) -> None:
        """Apply an on-disk change. Open buffers keep precedence over disk contents."""
        path = uri_path(uri)
        if path is None:
            return
        if deleted:
            # Clients may report only a deleted directory, not the files inside it.
            prefix = uri.rstrip("/") + "/"
            for known in [u for u in self.documents if u.startswith(prefix) or u == uri]:
                if known not in self.open_uris:
                    self.remove(known)
            return
        if uri not in self.open_uris and path.suffix.lower() in YAML_SUFFIXES:
            self.remove(uri)
            self.load(path)

    def document(self, uri: str) -> Document | None:
        """An indexed or open document, loading indexed files from disk on demand."""
        if uri not in self.documents and (path := uri_path(uri)):
            self.load(path)
        return self.documents.get(uri)

    def read(self, path: Path) -> Document | None:
        """A document by path, including files outside the index such as excluded ones."""
        uri = path.as_uri()
        if uri in self.documents:
            return self.documents[uri]
        try:
            return Document(uri, path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError):
            return None

    # Interpolations

    def resolve(self, expression: str, document: Document, entry: Entry) -> list[lsp.Location]:
        if expression.startswith("."):
            return self._resolve_relative(expression, document, entry)
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
        if not matches:
            return []
        matches.sort(
            key=lambda x: (-x[0], x[1].uri, x[1].range.start.line, x[1].range.start.character)
        )
        threshold = {
            "all": 0,
            "top matches only": matches[0][0],
            "perfect matches only": len(parts),
        }[self.settings.match_filter]
        return [location for level, location in matches if level >= threshold]

    def _resolve_relative(
        self, expression: str, document: Document, entry: Entry
    ) -> list[lsp.Location]:
        # `${.x}` is a sibling of the interpolating key; each extra dot goes up a level.
        levels = len(expression) - len(expression.lstrip("."))
        parent = entry.path[:-1]
        if levels > len(parent) + 1:
            return []
        target = (*parent[: len(parent) - levels + 1], *expression[levels:].split("."))
        local = [
            lsp.Location(document.uri, document.node_range(e.key))
            for e in document.entries
            if e.path == target
        ]
        if local:
            return local
        # Otherwise the key may come from another file placed in the same package.
        path = uri_path(document.uri)
        package = self.package(document, path) if path else ()
        return [d.location for d in self.index.get((*package, *target), [])]

    # Defaults lists

    def default_paths(self, document: Document, default: Default) -> list[Path]:
        source = uri_path(document.uri)
        root = self.root_for(source) if source else None
        if source is None or root is None or "${" in default.name:
            return []
        name = default.name.lstrip("/")
        if Path(name).suffix.lower() in YAML_SUFFIXES:
            name = str(Path(name).with_suffix(""))
        configured = self.config_roots(root)
        ancestors = [p for p in (source.parent, *source.parent.parents) if p.is_relative_to(root)]
        if default.name.startswith("/"):
            own = hydra.config_root(source, root, configured)
            conventional = [p for p in ancestors if p.name in hydra.CONVENTIONAL_ROOTS]
            bases = [*([own] if own else []), *(configured or conventional or [root])]
        else:
            bases = [*ancestors, *configured]
        result = []
        for base in bases:
            for suffix in YAML_SUFFIXES:
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
