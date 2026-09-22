"""Stdio LSP adapter for Neovim/LazyVim and other LSP clients."""

from __future__ import annotations

import asyncio
import functools
import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from lsprotocol import types as lsp
from pygls.lsp.server import LanguageServer
from pygls.protocol import LanguageServerProtocol, lsp_method

from hydradex import HydraDex, Settings, __version__
from hydradex.document import uri_path
from hydradex.python import BackendError

log = logging.getLogger(__name__)


class UTF16Protocol(LanguageServerProtocol):
    @lsp_method(lsp.INITIALIZE)
    def lsp_initialize(self, params: lsp.InitializeParams):
        # UTF-16 is mandatory in LSP. Select it consistently with the library API.
        if params.capabilities.general is None:
            params.capabilities.general = lsp.GeneralClientCapabilities()
        params.capabilities.general.position_encodings = [lsp.PositionEncodingKind.Utf16]
        return (yield from super().lsp_initialize(params))


class HydraLanguageServer(LanguageServer):
    def __init__(self) -> None:
        super().__init__("hydradex", __version__, protocol_cls=UTF16Protocol)
        self.engine: HydraDex | None = None
        self.worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="hydradex")
        self.operations = asyncio.Lock()

    async def run(self, function, *args):
        return await asyncio.get_running_loop().run_in_executor(
            self.worker, functools.partial(function, *args)
        )

    async def publish(self, uri: str) -> None:
        if self.engine is None:
            return
        document = self.engine.document(uri)
        if document is None:
            return
        version = document.version
        try:
            diagnostics = await self.run(self.engine.diagnostics, uri)
        except BackendError as exc:
            log.warning("%s", exc)
            diagnostics = [
                lsp.Diagnostic(
                    range=lsp.Range(lsp.Position(0, 0), lsp.Position(0, 0)),
                    message=str(exc),
                    severity=lsp.DiagnosticSeverity.Warning,
                    source="hydradex",
                    code="backend-unavailable",
                )
            ]
        current = self.engine.document(uri)
        if uri in self.engine.workspace.open_uris and current and current.version == version:
            self.text_document_publish_diagnostics(
                lsp.PublishDiagnosticsParams(uri, diagnostics, version=version)
            )

    async def replace(self, roots: list[Path], settings: Settings) -> None:
        overlays = []
        if self.engine:
            overlays = [
                self.engine.workspace.documents[uri] for uri in self.engine.workspace.open_uris
            ]
            await self.run(self.engine.shutdown)
        self.engine = await self.run(HydraDex, roots, settings)
        for doc in overlays:
            await self.run(self.engine.update, doc.uri, doc.text, doc.version)

    def cleanup(self) -> None:
        if self.engine:
            self.worker.submit(self.engine.shutdown).result()
        self.worker.shutdown(wait=True, cancel_futures=True)


def create_server() -> HydraLanguageServer:
    server = HydraLanguageServer()

    def serialized(function):
        @functools.wraps(function)
        async def handler(*args, **kwargs):
            async with server.operations:
                return await function(*args, **kwargs)

        return handler

    @server.feature(lsp.INITIALIZE)
    @serialized
    async def initialize(params: lsp.InitializeParams) -> None:
        roots = [
            path
            for folder in params.workspace_folders or []
            if (path := uri_path(folder.uri)) is not None
        ]
        if not roots and params.root_uri:
            if root := uri_path(params.root_uri):
                roots = [root]
        if not roots and params.root_path:
            roots = [Path(params.root_path)]
        await server.replace(roots, Settings.from_lsp(params.initialization_options))

    @server.feature(lsp.INITIALIZED)
    async def initialized(params: lsp.InitializedParams) -> None:
        capabilities = server.client_capabilities.workspace
        watched = capabilities.did_change_watched_files if capabilities else None
        if watched and watched.dynamic_registration:
            await server.client_register_capability_async(
                lsp.RegistrationParams(
                    [
                        lsp.Registration(
                            "hydradex-watch",
                            lsp.WORKSPACE_DID_CHANGE_WATCHED_FILES,
                            lsp.DidChangeWatchedFilesRegistrationOptions(
                                [lsp.FileSystemWatcher("**/*.{yaml,yml,py,pyi}")]
                            ),
                        )
                    ]
                )
            )

    async def sync(uri: str) -> None:
        if server.engine:
            document = server.workspace.get_text_document(uri)
            await server.run(server.engine.update, uri, document.source, document.version)
            await server.publish(uri)

    @server.feature(lsp.TEXT_DOCUMENT_DID_OPEN)
    @serialized
    async def opened(params: lsp.DidOpenTextDocumentParams) -> None:
        await sync(params.text_document.uri)

    @server.feature(lsp.TEXT_DOCUMENT_DID_CHANGE)
    @serialized
    async def changed(params: lsp.DidChangeTextDocumentParams) -> None:
        await sync(params.text_document.uri)

    @server.feature(lsp.TEXT_DOCUMENT_DID_SAVE)
    @serialized
    async def saved(params: lsp.DidSaveTextDocumentParams) -> None:
        await sync(params.text_document.uri)

    @server.feature(lsp.TEXT_DOCUMENT_DID_CLOSE)
    @serialized
    async def closed(params: lsp.DidCloseTextDocumentParams) -> None:
        if server.engine:
            await server.run(server.engine.close, params.text_document.uri)
        server.text_document_publish_diagnostics(
            lsp.PublishDiagnosticsParams(params.text_document.uri, [])
        )

    @server.feature(lsp.TEXT_DOCUMENT_DEFINITION)
    @serialized
    async def definitions(params: lsp.DefinitionParams):
        if server.engine is None:
            return []
        return await server.run(
            server.engine.definitions, params.text_document.uri, params.position
        )

    @server.feature(
        lsp.TEXT_DOCUMENT_COMPLETION, lsp.CompletionOptions(trigger_characters=[".", "_"])
    )
    @serialized
    async def completions(params: lsp.CompletionParams):
        if server.engine is None:
            return []
        return await server.run(
            server.engine.completions, params.text_document.uri, params.position
        )

    @server.feature(lsp.TEXT_DOCUMENT_HOVER)
    @serialized
    async def hover(params: lsp.HoverParams):
        if server.engine is None:
            return None
        return await server.run(server.engine.hover, params.text_document.uri, params.position)

    @server.feature(lsp.WORKSPACE_DID_CHANGE_WATCHED_FILES)
    @serialized
    async def files_changed(params: lsp.DidChangeWatchedFilesParams) -> None:
        if server.engine is None:
            return
        for event in params.changes:
            path = uri_path(event.uri)
            if path and path.suffix in (".py", ".pyi"):
                await server.run(server.engine.shutdown)
            else:
                await server.run(
                    functools.partial(
                        server.engine.workspace.changed,
                        event.uri,
                        deleted=event.type == lsp.FileChangeType.Deleted,
                    )
                )
        for uri in list(server.engine.workspace.open_uris):
            await server.publish(uri)

    @server.feature(lsp.WORKSPACE_DID_CHANGE_CONFIGURATION)
    @serialized
    async def configuration(params: lsp.DidChangeConfigurationParams) -> None:
        if server.engine is None:
            return
        try:
            settings = Settings.from_lsp(params.settings)
        except (ValueError, TypeError) as exc:
            server.window_show_message(lsp.ShowMessageParams(lsp.MessageType.Error, str(exc)))
            return
        await server.replace(server.engine.workspace.roots, settings)
        for uri in list(server.engine.workspace.open_uris):
            await server.publish(uri)

    @server.feature(lsp.WORKSPACE_DID_CHANGE_WORKSPACE_FOLDERS)
    @serialized
    async def folders(params: lsp.DidChangeWorkspaceFoldersParams) -> None:
        if server.engine is None:
            return
        removed = {uri_path(folder.uri) for folder in params.event.removed}
        roots = [root for root in server.engine.workspace.roots if root not in removed]
        roots.extend(
            path for folder in params.event.added if (path := uri_path(folder.uri)) is not None
        )
        await server.replace(roots, server.engine.settings)

    @server.command("hydradex.refreshIndex")
    @serialized
    async def refresh(arguments: list[Any] | None = None) -> None:
        if server.engine:
            await server.run(server.engine.refresh)
            for uri in list(server.engine.workspace.open_uris):
                await server.publish(uri)

    @server.feature(lsp.SHUTDOWN)
    @serialized
    async def shutdown(*args: Any) -> None:
        if server.engine:
            await server.run(server.engine.shutdown)

    return server
