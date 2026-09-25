"""Python analysis through a managed ty language-server process.

Targets are analysed by writing small snippets such as ``import pkg\\npkg.Class`` to
a single in-memory document and querying ty at the end of it. Nothing is written to
disk and target modules are never imported. Results are cached until Python files
change, which keeps repeated diagnostics on unchanged targets free.
"""

from __future__ import annotations

import keyword
import logging
import os
import shutil
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any
from uuid import uuid4

from lsprotocol import types as lsp

from hydradex.document import utf16
from hydradex.lsp_client import BackendError, LspClient
from hydradex.settings import Settings

__all__ = ["BackendError", "PythonAnalysis", "valid_target"]

log = logging.getLogger(__name__)

_KINDS = {
    lsp.CompletionItemKind.Module: "module",
    lsp.CompletionItemKind.Class: "class",
    lsp.CompletionItemKind.Function: "function",
    lsp.CompletionItemKind.Method: "function",
}


def valid_target(target: str, *, partial: bool = False) -> bool:
    parts = target.split(".")
    if partial and not parts[-1]:
        parts.pop()
    return bool(parts) and all(p.isidentifier() and not keyword.iskeyword(p) for p in parts)


def backend_command(settings: Settings) -> list[str]:
    if settings.backend_command:
        return list(settings.backend_command)
    sibling = Path(sys.executable).parent / ("ty.exe" if os.name == "nt" else "ty")
    executable = str(sibling) if sibling.is_file() else shutil.which("ty")
    if executable is None:
        raise BackendError("ty executable not found; install it or set backendCommand")
    return [executable, "server"]


def _range(data: dict[str, Any]) -> lsp.Range:
    start, end = data["start"], data["end"]
    return lsp.Range(
        lsp.Position(start["line"], start["character"]),
        lsp.Position(end["line"], end["character"]),
    )


class PythonAnalysis:
    """One ty process for one Python project root. Not thread-safe."""

    def __init__(self, root: Path, settings: Settings):
        self.root = root
        self.settings = settings
        self.shadow_uri = (root / f"__hydradex_{uuid4().hex}.py").as_uri()
        self._version = 0
        self._cache: dict[tuple[str, str], Any] = {}
        self._modules: dict[str, bool] = {}
        self.options = self._options()
        self.client = LspClient(
            backend_command(settings),
            root,
            on_request=self._on_request,
            on_notification=self._on_notification,
        )
        try:
            self.client.request(
                "initialize",
                {
                    "processId": os.getpid(),
                    "rootUri": root.as_uri(),
                    "workspaceFolders": [{"uri": root.as_uri(), "name": root.name}],
                    "capabilities": {
                        "general": {"positionEncodings": ["utf-16"]},
                        "workspace": {"configuration": True},
                    },
                    "initializationOptions": self.options,
                },
                settings.backend_timeout,
            )
            self.client.notify("initialized", {})
        except BaseException:
            self.client.close()
            raise

    @property
    def alive(self) -> bool:
        return self.client.alive

    def close(self) -> None:
        self.client.close()

    def _options(self) -> dict[str, Any]:
        paths = [
            str(self.root),
            *([str(self.root / "src")] if (self.root / "src").is_dir() else []),
            *map(str, self.settings.paths(self.settings.extra_paths, self.root)),
        ]
        environment: dict[str, Any] = {"extra-paths": paths}
        if self.settings.python_path:
            python = Path(self.settings.python_path).expanduser()
            environment["python"] = str((self.root / python).resolve())
        return {
            "configuration": {"environment": environment},
            "diagnosticMode": "off",
            "completions": {"autoImport": False},
        }

    def _on_request(self, method: str, params: Any) -> Any:
        if method == "workspace/configuration":
            return [self.options for _ in (params or {}).get("items", [])]
        return None

    def _on_notification(self, method: str, params: Any) -> None:
        if method in ("window/logMessage", "window/showMessage"):
            log.debug("ty: %s", (params or {}).get("message"))

    def files_changed(self, events: Iterable[lsp.FileEvent]) -> None:
        """Forward on-disk Python changes to ty and invalidate cached answers."""
        changes = [{"uri": event.uri, "type": int(event.type)} for event in events]
        if not changes:
            return
        self._cache.clear()
        self._modules.clear()
        self.client.notify("workspace/didChangeWatchedFiles", {"changes": changes})

    def _query(self, code: str, method: str) -> Any:
        key = (method, code)
        if key in self._cache:
            return self._cache[key]
        self._version += 1
        if self._version == 1:
            self.client.notify(
                "textDocument/didOpen",
                {
                    "textDocument": {
                        "uri": self.shadow_uri,
                        "languageId": "python",
                        "version": self._version,
                        "text": code,
                    }
                },
            )
        else:
            self.client.notify(
                "textDocument/didChange",
                {
                    "textDocument": {"uri": self.shadow_uri, "version": self._version},
                    "contentChanges": [{"text": code}],
                },
            )
        last = code.split("\n")
        result = self.client.request(
            method,
            {
                "textDocument": {"uri": self.shadow_uri},
                "position": {"line": len(last) - 1, "character": utf16(last[-1])},
            },
            self.settings.backend_timeout,
        )
        self._cache[key] = result
        return result

    def _code(self, target: str) -> str:
        """Import the longest real module prefix, leaving attributes in the expression.

        `from package import module` does not reliably populate package attributes in
        ty, especially in src-layout projects, so modules are imported directly.
        """
        parts = target.split(".")
        for length in range(len(parts) - 1, 0, -1):
            module = ".".join(parts[:length])
            if module not in self._modules:
                probe = f"import {module} as _hydradex_module\n_hydradex_module"
                found = self._locations(self._query(probe, "textDocument/definition"))
                self._modules[module] = bool(found)
            if self._modules[module]:
                return f"import {module}\n{target}"
        return f"import {parts[0]}\n{target}"

    def _locations(self, result: Any) -> list[lsp.Location]:
        if result is None:
            return []
        if isinstance(result, dict):
            result = [result]
        locations = []
        for item in result:
            if "targetUri" in item:
                uri = item["targetUri"]
                span = item.get("targetSelectionRange") or item["targetRange"]
            else:
                uri, span = item["uri"], item["range"]
            if uri != self.shadow_uri:
                locations.append(lsp.Location(uri, _range(span)))
        return locations

    @staticmethod
    def _items(result: Any) -> list[dict[str, Any]]:
        if isinstance(result, dict):
            return result.get("items", [])
        return result or []

    def definitions(self, target: str) -> list[lsp.Location]:
        if not valid_target(target):
            return []
        return self._locations(self._query(self._code(target), "textDocument/definition"))

    def resolved(self, target: str) -> bool:
        return bool(self.definitions(target))

    def complete(self, prefix: str) -> list[tuple[str, str, str]]:
        """Dotted-name completions as (qualified name, kind, detail)."""
        if prefix and not valid_target(prefix, partial=True):
            return []
        code = f"import {prefix}" if "." not in prefix else self._code(prefix)
        parent, _, stem = prefix.rpartition(".")
        result = []
        for item in self._items(self._query(code, "textDocument/completion")):
            name = item.get("label", "")
            kind = _KINDS.get(item.get("kind"))  # type: ignore[arg-type]
            if kind and name.isidentifier() and name.startswith(stem):
                qualified = f"{parent}.{name}" if parent else name
                result.append((qualified, kind, item.get("detail") or name))
        return result

    def parameters(self, target: str) -> list[tuple[str, str]]:
        """Keyword parameters as (name, detail); ty omits self, /, *args, and **kwargs."""
        if not valid_target(target):
            return []
        code = self._code(target) + "("
        result: dict[str, str] = {}
        for item in self._items(self._query(code, "textDocument/completion")):
            label = item.get("label", "").strip()
            name = label.rstrip("= ")
            if label.endswith("=") and name.isidentifier():
                result.setdefault(name, item.get("detail") or name)
        return list(result.items())

    def hover(self, target: str) -> str | None:
        if not valid_target(target):
            return None
        response = self._query(self._code(target), "textDocument/hover")
        if not response:
            return None
        contents = response.get("contents")
        if not isinstance(contents, list):
            contents = [contents]
        parts = [
            item if isinstance(item, str) else (item or {}).get("value", "") for item in contents
        ]
        return "\n\n".join(part for part in parts if part) or None
