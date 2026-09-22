"""Python analysis through a managed ty language-server process.

Pygls handles JSON-RPC, framing, and LSP types. A dedicated event loop bridges its
async client to the synchronous library API. Shadow documents live only in memory.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import keyword
import logging
import os
import shutil
import sys
import threading
from collections.abc import Coroutine
from pathlib import Path
from typing import Any, TypeVar
from uuid import uuid4

from lsprotocol import types as lsp
from pygls.lsp.client import BaseLanguageClient
from pygls.protocol import default_converter

from hydradex.document import utf16
from hydradex.settings import Settings

log = logging.getLogger(__name__)
T = TypeVar("T")


def backend_converter():
    converter = default_converter()

    def initialize_result(data: dict[str, Any], _: type) -> lsp.InitializeResult:
        capabilities = dict(data["capabilities"])
        # lsprotocol 2025 cannot decode cell-only notebook selectors. This client
        # negotiates only text documents, so notebook capabilities are irrelevant.
        capabilities.pop("notebookDocumentSync", None)
        return lsp.InitializeResult(converter.structure(capabilities, lsp.ServerCapabilities))

    converter.register_structure_hook(lsp.InitializeResult, initialize_result)
    return converter


class BackendError(RuntimeError):
    """The configured Python backend failed or exceeded its request deadline."""


def valid_target(target: str, *, partial: bool = False) -> bool:
    parts = target.split(".")
    if partial and not parts[-1]:
        parts.pop()
    return bool(parts) and all(p.isidentifier() and not keyword.iskeyword(p) for p in parts)


class PythonAnalysis:
    def __init__(self, root: Path, settings: Settings):
        self.root = root
        self.settings = settings
        self.shadow_uri = (root / f"__hydradex_{uuid4().hex}.py").as_uri()
        self.client = BaseLanguageClient(
            "hydradex-python", "0.2.0", converter_factory=backend_converter
        )
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()
        self.closed = False
        self._modules: dict[str, bool] = {}
        try:
            self._run(self._start())
        except Exception:
            self.close()
            raise

    def _run(self, coroutine: Coroutine[Any, Any, T]) -> T:
        if self.closed:
            coroutine.close()
            raise BackendError("Python backend is closed")
        future = asyncio.run_coroutine_threadsafe(coroutine, self.loop)
        try:
            return future.result(timeout=self.settings.backend_timeout)
        except concurrent.futures.TimeoutError as exc:
            future.cancel()
            raise BackendError("Python backend request timed out") from exc
        except Exception as exc:
            raise BackendError(f"Python backend failed: {exc}") from exc

    async def _start(self) -> None:
        command = list(self.settings.backend_command)
        if not command:
            sibling = Path(sys.executable).parent / ("ty.exe" if os.name == "nt" else "ty")
            executable = str(sibling) if sibling.is_file() else shutil.which("ty")
            if executable is None:
                raise BackendError("ty executable not found; install it or set backendCommand")
            command = [executable, "server"]
        paths = [
            str(self.root),
            *([str(self.root / "src")] if (self.root / "src").is_dir() else []),
            *map(str, self.settings.paths(self.settings.extra_paths, self.root)),
        ]
        environment: dict[str, Any] = {"extra-paths": paths}
        if self.settings.python_path:
            environment["python"] = str(
                (self.root / Path(self.settings.python_path).expanduser()).resolve()
            )
        options = {
            "configuration": {"environment": environment},
            "diagnosticMode": "off",
            "completions": {"autoImport": False},
        }

        @self.client.feature(lsp.WORKSPACE_CONFIGURATION)
        def configuration(params: lsp.ConfigurationParams) -> list[Any]:
            return [options for _ in params.items]

        @self.client.feature(lsp.CLIENT_REGISTER_CAPABILITY)
        def register(params: lsp.RegistrationParams) -> None:
            pass

        @self.client.feature(lsp.TEXT_DOCUMENT_PUBLISH_DIAGNOSTICS)
        def diagnostics(params: lsp.PublishDiagnosticsParams) -> None:
            pass

        @self.client.feature(lsp.WINDOW_LOG_MESSAGE)
        def message(params: lsp.LogMessageParams) -> None:
            log.debug("ty: %s", params.message)

        @self.client.feature(lsp.WINDOW_SHOW_MESSAGE)
        def show_message(params: lsp.ShowMessageParams) -> None:
            log.warning("ty: %s", params.message)

        await self.client.start_io(*command, cwd=self.root)
        self.client._async_tasks.append(asyncio.create_task(self._drain_stderr()))
        await self.client.initialize_async(
            lsp.InitializeParams(
                process_id=os.getpid(),
                root_uri=self.root.as_uri(),
                capabilities=lsp.ClientCapabilities(
                    general=lsp.GeneralClientCapabilities(
                        position_encodings=[lsp.PositionEncodingKind.Utf16]
                    ),
                    workspace=lsp.WorkspaceClientCapabilities(configuration=True),
                ),
                workspace_folders=[lsp.WorkspaceFolder(self.root.as_uri(), self.root.name)],
                initialization_options=options,
            )
        )
        self.client.initialized(lsp.InitializedParams())

    async def _drain_stderr(self) -> None:
        process = self.client._server
        if process is not None and process.stderr is not None:
            while data := await process.stderr.read(4096):
                log.debug("Python backend: %s", data.decode(errors="replace").rstrip())

    async def _stop(self) -> None:
        process = self.client._server
        if process is not None and process.returncode is None:
            try:
                await asyncio.wait_for(self.client.shutdown_async(None), timeout=2)
                self.client.exit(None)
                await asyncio.wait_for(process.wait(), timeout=2)
            except (Exception, asyncio.CancelledError):
                if process.returncode is None:
                    process.kill()
                    await process.wait()
        await self.client.stop()

    def close(self) -> None:
        if self.closed:
            return
        try:
            future = asyncio.run_coroutine_threadsafe(self._stop(), self.loop)
            future.result(timeout=6)
        finally:
            self.closed = True
            self.loop.call_soon_threadsafe(self.loop.stop)
            self.thread.join(timeout=2)
            self.loop.close()

    def code(self, target: str) -> str:
        parts = target.split(".")
        # Import the longest real module, leaving class/method attributes in the
        # expression. `from package import module` does not reliably populate the
        # package attribute in ty, especially in src-layout projects.
        for length in range(len(parts) - 1, 0, -1):
            module = ".".join(parts[:length])
            if module not in self._modules:
                probe = f"import {module} as _hydradex_module\n_hydradex_module"
                response = self._run(self._query(probe, lsp.TEXT_DOCUMENT_DEFINITION))
                self._modules[module] = bool(self.locations(response))
            if self._modules[module]:
                return f"import {module}\n{target}"
        return f"import {parts[0]}\n{target}"

    async def _query(self, code: str, method: str) -> Any:
        uri = self.shadow_uri
        self.client.text_document_did_open(
            lsp.DidOpenTextDocumentParams(lsp.TextDocumentItem(uri, "python", 1, code))
        )
        position = lsp.Position(len(code.split("\n")) - 1, utf16(code.split("\n")[-1]))
        params_type = {
            lsp.TEXT_DOCUMENT_DEFINITION: lsp.DefinitionParams,
            lsp.TEXT_DOCUMENT_COMPLETION: lsp.CompletionParams,
            lsp.TEXT_DOCUMENT_HOVER: lsp.HoverParams,
        }[method]
        params = params_type(lsp.TextDocumentIdentifier(uri), position)
        try:
            return await self.client.protocol.send_request_async(method, params)
        finally:
            self.client.text_document_did_close(
                lsp.DidCloseTextDocumentParams(lsp.TextDocumentIdentifier(uri))
            )

    def definitions(self, target: str) -> list[lsp.Location]:
        if not valid_target(target):
            return []
        result = self._run(self._query(self.code(target), lsp.TEXT_DOCUMENT_DEFINITION))
        return self.locations(result)

    def locations(self, result: Any) -> list[lsp.Location]:
        if result is None:
            return []
        if isinstance(result, lsp.Location):
            result = [result]
        locations = [
            lsp.Location(item.target_uri, item.target_selection_range)
            if isinstance(item, lsp.LocationLink)
            else item
            for item in result
        ]
        return [location for location in locations if location.uri != self.shadow_uri]

    def complete(self, prefix: str) -> list[tuple[str, str, str]]:
        if prefix and not valid_target(prefix, partial=True):
            return []
        code = f"import {prefix}" if "." not in prefix else self.code(prefix)
        response = self._run(self._query(code, lsp.TEXT_DOCUMENT_COMPLETION))
        items = response.items if isinstance(response, lsp.CompletionList) else response or []
        kinds = {
            lsp.CompletionItemKind.Module: "module",
            lsp.CompletionItemKind.Class: "class",
            lsp.CompletionItemKind.Function: "function",
            lsp.CompletionItemKind.Method: "function",
        }
        parent = prefix.rpartition(".")[0]
        result = []
        for item in items:
            name = item.label
            if (
                name.isidentifier()
                and name.startswith(prefix.rsplit(".", 1)[-1])
                and item.kind in kinds
            ):
                result.append(
                    (f"{parent}.{name}" if parent else name, kinds[item.kind], item.detail or name)
                )
        return result

    def parameters(self, target: str) -> list[tuple[str, str]]:
        if not valid_target(target):
            return []
        code = self.code(target) + "("
        response = self._run(self._query(code, lsp.TEXT_DOCUMENT_COMPLETION))
        items = response.items if isinstance(response, lsp.CompletionList) else response or []
        result: dict[str, str] = {}
        for item in items:
            # Keyword argument completions already account for self, /, *args, and **kwargs.
            label = item.label.strip()
            name = label.rstrip("= ")
            if label.endswith("=") and name.isidentifier():
                result[name] = item.detail or name
        return list(result.items())

    def hover(self, target: str) -> str | None:
        if not valid_target(target):
            return None
        response = self._run(self._query(self.code(target), lsp.TEXT_DOCUMENT_HOVER))
        if response is None:
            return None
        contents = response.contents
        if isinstance(contents, lsp.MarkupContent):
            return contents.value
        if not isinstance(contents, list):
            contents = [contents]
        return "\n\n".join(item if isinstance(item, str) else item.value for item in contents)

    def resolved(self, target: str) -> bool:
        return bool(self.definitions(target))
