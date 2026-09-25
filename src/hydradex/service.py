"""Public, synchronous library facade. Instances are owned by one calling thread."""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Iterable
from pathlib import Path

from lsprotocol import types as lsp
from yaml.nodes import ScalarNode

from hydradex.composition import compose
from hydradex.document import Document, defaults, uri_path
from hydradex.python import BackendError, PythonAnalysis, valid_target
from hydradex.settings import Settings
from hydradex.workspace import Workspace

log = logging.getLogger(__name__)

PROJECT_MARKERS = ("pyproject.toml", "setup.py", "setup.cfg", ".git")
PYTHON_SUFFIXES = (".py", ".pyi")
RETRY_DELAY = 30.0
_TARGET_LINE = re.compile(r"""\s*(?:-\s+)?["']?_target_["']?\s*:\s*["']?([\w.]*)""")
_COMPLETION_KINDS = {
    "module": lsp.CompletionItemKind.Module,
    "class": lsp.CompletionItemKind.Class,
    "function": lsp.CompletionItemKind.Function,
}


def is_dynamic(target: str) -> bool:
    """Runtime interpolations and OmegaConf's mandatory `???` cannot be checked statically."""
    return "${" in target or target == "???"


class HydraDex:
    def __init__(self, roots: Iterable[Path | str], settings: Settings | None = None):
        self.settings = settings or Settings()
        self.workspace = Workspace([Path(root) for root in roots], self.settings)
        self._python: dict[Path, PythonAnalysis] = {}
        self._failures: dict[Path, tuple[float, BackendError]] = {}
        self.workspace.refresh()

    @property
    def roots(self) -> list[Path]:
        return list(self.workspace.roots)

    # Lifecycle

    def reconfigure(
        self, *, roots: Iterable[Path | str] | None = None, settings: Settings | None = None
    ) -> None:
        """Replace roots and/or settings, keeping open buffers."""
        overlays = [self.workspace.documents[uri] for uri in self.workspace.open_uris]
        self.shutdown()
        self.settings = settings or self.settings
        roots = [Path(root) for root in roots] if roots is not None else self.workspace.roots
        self.workspace = Workspace(roots, self.settings)
        self.workspace.refresh()
        for document in overlays:
            self.workspace.put(document, opened=True)

    def refresh(self) -> None:
        """Rescan YAML files, retaining open buffers, and restart Python backends."""
        self.workspace.refresh()
        self.shutdown()

    def shutdown(self) -> None:
        """Release Python backend processes. They restart lazily when needed."""
        backends, self._python = self._python, {}
        for backend in backends.values():
            backend.close()
        self._failures.clear()

    def __enter__(self) -> HydraDex:
        return self

    def __exit__(self, *args: object) -> None:
        self.shutdown()

    # Documents and files

    def update(self, uri: str, text: str, version: int | None = None) -> None:
        """Open or update a buffer. Its content takes precedence over the file on disk."""
        self.workspace.put(Document(uri, text, version), opened=True)

    def close(self, uri: str) -> None:
        """Discard a buffer and restore the current disk version."""
        self.workspace.close(uri)

    def document(self, uri: str) -> Document | None:
        return self.workspace.document(uri)

    def files_changed(self, events: Iterable[lsp.FileEvent]) -> None:
        """Apply on-disk YAML changes and forward Python changes to running backends."""
        python = []
        for event in events:
            path = uri_path(event.uri)
            if path is not None and path.suffix in PYTHON_SUFFIXES:
                python.append(event)
            else:
                self.workspace.changed(event.uri, deleted=event.type == lsp.FileChangeType.Deleted)
        if python:
            self._failures.clear()
            for root, backend in list(self._python.items()):
                try:
                    backend.files_changed(python)
                except BackendError:
                    self._python.pop(root).close()

    # Python backends

    def project_root(self, uri: str) -> Path:
        path = uri_path(uri)
        if path is None:
            return Path.cwd().resolve()
        root = self.workspace.root_for(path)
        if root is not None:
            return root
        marked = (p for p in path.parents if any((p / m).exists() for m in PROJECT_MARKERS))
        return next(marked, path.parent)

    def python(self, uri: str) -> PythonAnalysis:
        root = self.project_root(uri)
        backend = self._python.get(root)
        if backend is not None and not backend.alive:
            self._python.pop(root).close()
            backend = None
        if backend is None:
            failure = self._failures.get(root)
            if failure and time.monotonic() - failure[0] < RETRY_DELAY:
                raise failure[1]
            try:
                backend = PythonAnalysis(root, self.settings)
            except BackendError as exc:
                self._failures[root] = (time.monotonic(), exc)
                raise
            self._failures.pop(root, None)
            self._python[root] = backend
        return backend

    def python_targets(self, uri: str) -> list[str]:
        """Statically resolvable `_target_` strings, for diagnostics warm-up."""
        document = self.document(uri)
        if document is None:
            return []
        targets = (e.value.value for e in document.targets() if isinstance(e.value, ScalarNode))
        return list(dict.fromkeys(t for t in targets if not is_dynamic(t) and valid_target(t)))

    def target_resolved(self, uri: str, target: str) -> bool:
        return valid_target(target) and self.python(uri).resolved(target)

    # Features

    def definitions(self, uri: str, position: lsp.Position) -> list[lsp.Location]:
        document = self.document(uri)
        if document is None:
            return []
        if interpolation := document.interpolation_at(position):
            return self.workspace.resolve(interpolation[0], document, interpolation[1])
        target = document.target_at(position)
        if target and isinstance(target.value, ScalarNode):
            return self.python(uri).definitions(target.value.value)
        for default in defaults(document):
            if document.contains(default.node, position):
                location = self.workspace.default_location(document, default)
                return [location] if location else []
        return []

    def completions(self, uri: str, position: lsp.Position) -> list[lsp.CompletionItem]:
        document = self.document(uri)
        if document is None or not 0 <= position.line < len(document.lines):
            return []
        items = self._target_completions(uri, document, position)
        if items is not None:
            return items
        return self._key_completions(uri, document, position)

    def _target_completions(
        self, uri: str, document: Document, position: lsp.Position
    ) -> list[lsp.CompletionItem] | None:
        line = document.lines[position.line]
        column = document.column(position)
        match = _TARGET_LINE.match(line)
        if not match or not match.start(1) <= column <= match.end(1):
            return None
        # A matching line inside a block scalar is text, not a key. While the file is
        # being edited it may not parse, so only valid YAML is checked strictly.
        if not document.errors and not any(
            entry.key.start_mark is not None and entry.key.start_mark.line == position.line
            for entry in document.targets()
        ):
            return []
        prefix = line[match.start(1) : column]
        span = document.range(position.line, match.start(1), position.line, match.end(1))
        return [
            lsp.CompletionItem(
                label=name.rsplit(".", 1)[-1],
                kind=_COMPLETION_KINDS[kind],
                detail=detail,
                filter_text=name,
                text_edit=lsp.TextEdit(span, name),
            )
            for name, kind, detail in self.python(uri).complete(prefix)
        ]

    def _key_completions(
        self, uri: str, document: Document, position: lsp.Position
    ) -> list[lsp.CompletionItem]:
        context = document.key_context(position)
        if context is None:
            return []
        composed = compose(self.workspace, context.document)
        candidates: dict[str, str] = {}
        for path, value in composed.items():
            if path[:-1] != context.parent or value.document.uri == uri:
                continue
            node = value.entry.value
            preview = node.value if isinstance(node, ScalarNode) else "mapping/list"
            source = uri_path(value.document.uri)
            candidates[path[-1]] = (
                f"{preview} (from {source.name if source else value.document.uri})"
            )
        target = context.target
        inherited = composed.get((*context.parent, "_target_"))
        if target is None and inherited and isinstance(inherited.entry.value, ScalarNode):
            target = inherited.entry.value.value
        if target and not is_dynamic(target):
            candidates.update(self.python(uri).parameters(target))
        return [
            lsp.CompletionItem(
                label=name,
                kind=lsp.CompletionItemKind.Field,
                detail=description,
                text_edit=lsp.TextEdit(context.span, name if context.has_colon else name + ": "),
            )
            for name, description in candidates.items()
            if name not in context.siblings and name.startswith(context.prefix)
        ]

    def hover(self, uri: str, position: lsp.Position) -> lsp.Hover | None:
        document = self.document(uri)
        if document is None:
            return None
        target = document.target_at(position)
        if target and isinstance(target.value, ScalarNode):
            python = self.python(uri)
            # ty describes unresolvable names as "Unknown"; the diagnostic is clearer.
            text = python.hover(target.value.value) if python.resolved(target.value.value) else None
            if text:
                return lsp.Hover(
                    lsp.MarkupContent(lsp.MarkupKind.Markdown, text),
                    document.node_range(target.value),
                )
            return None
        for default in defaults(document):
            if document.contains(default.node, position):
                location = self.workspace.default_location(document, default)
                if location:
                    text = location.uri
                else:
                    text = f"{default.name} [{'optional, ' if default.optional else ''}not found]"
                return lsp.Hover(
                    lsp.MarkupContent(lsp.MarkupKind.PlainText, text),
                    document.node_range(default.node),
                )
        return None

    def diagnostics(self, uri: str) -> list[lsp.Diagnostic]:
        document = self.document(uri)
        if document is None:
            return []
        result = list(document.errors)
        seen: set[tuple[int, int]] = set()
        for entry in document.targets():
            node = entry.value
            start = (node.start_mark.line, node.start_mark.column)
            if start in seen:  # YAML aliases repeat the same node.
                continue
            seen.add(start)
            if isinstance(node, ScalarNode) and is_dynamic(node.value):
                continue
            target = node.value if isinstance(node, ScalarNode) else ""
            if not self.target_resolved(uri, target):
                result.append(
                    lsp.Diagnostic(
                        range=document.node_range(node),
                        message=f"Cannot resolve Python _target_ {target!r}"
                        if target
                        else "_target_ must be a dotted Python path",
                        severity=lsp.DiagnosticSeverity.Error,
                        source="hydradex",
                        code="unresolved-target",
                    )
                )
        return result
