"""Public, synchronous library facade. Instances are owned by one calling thread."""

from __future__ import annotations

import re
from pathlib import Path

from lsprotocol import types as lsp
from yaml.nodes import ScalarNode

from hydradex.composition import compose
from hydradex.document import Document, defaults, uri_path
from hydradex.python import PythonAnalysis, valid_target
from hydradex.settings import Settings
from hydradex.workspace import Workspace


class HydraDex:
    def __init__(self, roots: list[Path | str], settings: Settings | None = None):
        self.settings = settings or Settings()
        self.workspace = Workspace([Path(root) for root in roots], self.settings)
        self._python: dict[Path, PythonAnalysis] = {}
        self.refresh()

    def refresh(self) -> None:
        """Rescan YAML files, retaining unsaved buffers, and reset Python projects."""
        self.workspace.refresh()
        self.shutdown()

    def shutdown(self) -> None:
        """Release Python backend processes. The library can lazily restart them."""
        for backend in self._python.values():
            backend.close()
        self._python.clear()

    def __enter__(self) -> HydraDex:
        return self

    def __exit__(self, *args: object) -> None:
        self.shutdown()

    def update(self, uri: str, text: str, version: int | None = None) -> None:
        """Open/update a buffer. Its content takes precedence over the file on disk."""
        self.workspace.put(Document(uri, text, version), opened=True)

    def close(self, uri: str) -> None:
        """Discard a buffer overlay and restore the current disk version."""
        self.workspace.close(uri)

    def document(self, uri: str) -> Document | None:
        if uri not in self.workspace.documents:
            path = uri_path(uri)
            if path:
                self.workspace.load(path)
        return self.workspace.documents.get(uri)

    def python(self, uri: str) -> PythonAnalysis:
        path = uri_path(uri)
        root = self.workspace.root_for(path) if path else None
        root = root or (path.parent if path else Path.cwd())
        backend = self._python.get(root)
        if backend is not None:
            process = backend.client._server
            if (
                backend.closed
                or backend.client.stopped
                or (process is not None and process.returncode is not None)
            ):
                self._python.pop(root).close()
        if root not in self._python:
            self._python[root] = PythonAnalysis(root, self.settings)
        return self._python[root]

    def definitions(self, uri: str, position: lsp.Position) -> list[lsp.Location]:
        document = self.document(uri)
        if document is None:
            return []
        interpolation = document.interpolation_at(position)
        if interpolation:
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
        line = document.lines[position.line]
        column = document.column(position)
        match = re.match(r"""\s*(?:-\s+)?["']?_target_["']?\s*:\s*["']?([\w.]*)""", line)
        if match and match.start(1) <= column <= match.end(1):
            if not document.errors and not any(
                entry.key.value == "_target_"
                and entry.key.start_mark is not None
                and entry.key.start_mark.line == position.line
                for entry in document.entries
            ):
                return []
            prefix = line[match.start(1) : column]
            span = document.range(position.line, match.start(1), position.line, match.end(1))
            kinds = {
                "module": lsp.CompletionItemKind.Module,
                "class": lsp.CompletionItemKind.Class,
                "function": lsp.CompletionItemKind.Function,
            }
            return [
                lsp.CompletionItem(
                    label=name.rsplit(".", 1)[-1],
                    kind=kinds[kind],
                    detail=detail,
                    filter_text=name,
                    text_edit=lsp.TextEdit(span, name),
                )
                for name, kind, detail in self.python(uri).complete(prefix)
            ]
        context = document.key_context(position)
        if context is None:
            return []
        composed = compose(self.workspace, context.document)
        inherited = {
            path[-1]: value
            for path, value in composed.items()
            if path[:-1] == context.parent and value.document.uri != uri
        }
        candidates = {}
        for name, value in inherited.items():
            node = value.entry.value
            preview = node.value if isinstance(node, ScalarNode) else "mapping/list"
            path = uri_path(value.document.uri)
            candidates[name] = f"{preview} (from {path.name if path else value.document.uri})"
        target_entry = composed.get((*context.parent, "_target_"))
        target = context.target
        if target_entry and isinstance(target_entry.entry.value, ScalarNode):
            target = target_entry.entry.value.value
        if target:
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
            text = self.python(uri).hover(target.value.value)
            if text:
                return lsp.Hover(
                    lsp.MarkupContent(lsp.MarkupKind.Markdown, text),
                    document.node_range(target.value),
                )
        for default in defaults(document):
            if document.contains(default.node, position):
                location = self.workspace.default_location(document, default)
                text = location.uri if location else f"{default.name} [not found]"
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
        for entry in document.entries:
            if entry.key.value != "_target_":
                continue
            node = entry.value
            start = (node.start_mark.line, node.start_mark.column)
            if start in seen:
                continue
            seen.add(start)
            if isinstance(node, ScalarNode) and "${" in node.value:
                continue  # Runtime interpolations cannot be statically resolved.
            if (
                not isinstance(node, ScalarNode)
                or not valid_target(node.value)
                or not self.python(uri).resolved(node.value)
            ):
                result.append(
                    lsp.Diagnostic(
                        range=document.node_range(node),
                        message="Cannot resolve Python _target_",
                        severity=lsp.DiagnosticSeverity.Error,
                        source="hydradex",
                        code="unresolved-target",
                    )
                )
        return result
