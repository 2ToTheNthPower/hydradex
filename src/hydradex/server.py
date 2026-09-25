"""Stdio LSP adapter for Neovim/LazyVim and other LSP clients.

Concurrency model: the `HydraDex` engine is owned by a single worker thread, and
every handler submits its work to that worker before its first `await`. pygls
starts async handlers in message order, so edits and requests are applied in the
order the client sent them, without a global lock. Diagnostics are debounced per
document and split into one job per `_target_`, so completion and hover requests
interleave with long-running analysis instead of waiting behind it.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, TypeVar

from lsprotocol import types as lsp
from pygls.lsp.server import LanguageServer
from pygls.protocol import LanguageServerProtocol, lsp_method

from hydradex import __version__
from hydradex.document import uri_path
from hydradex.python import BackendError
from hydradex.service import HydraDex
from hydradex.settings import Settings, settings_section

log = logging.getLogger(__name__)
T = TypeVar("T")


class UTF16Protocol(LanguageServerProtocol):
    @lsp_method(lsp.INITIALIZE)
    def lsp_initialize(self, params: lsp.InitializeParams):
        # UTF-16 is mandatory in LSP. Select it consistently with the library API.
        if params.capabilities.general is None:
            params.capabilities.general = lsp.GeneralClientCapabilities()
        params.capabilities.general.position_encodings = [lsp.PositionEncodingKind.Utf16]
        return (yield from super().lsp_initialize(params))


class HydraLanguageServer(LanguageServer):
    diagnostic_delay = 0.15

    def __init__(self) -> None:
        super().__init__("hydradex", __version__, protocol_cls=UTF16Protocol)
        # Only touched from the worker thread.
        self.engine: HydraDex | None = None
        self.worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="hydradex")
        # Only touched from the event loop thread.
        self.initial_settings = Settings()
        self.settings = Settings()
        self.roots: list[Path] = []
        self.open_uris: set[str] = set()
        self.messages: list[lsp.ShowMessageParams] = []
        self.pending: dict[str, asyncio.Task[None]] = {}

    def run(self, function: Callable[[HydraDex], T], default: T) -> asyncio.Future[T]:
        """Schedule `function(engine)` on the worker; must be called before any await."""

        def job() -> T:
            return default if self.engine is None else function(self.engine)

        return asyncio.get_running_loop().run_in_executor(self.worker, job)

    def warn(self, message: str, kind: lsp.MessageType = lsp.MessageType.Warning) -> None:
        log.warning("%s", message)
        self.window_show_message(lsp.ShowMessageParams(kind, message))

    # Diagnostics

    def schedule(self, uri: str, delay: float | None = None) -> None:
        if task := self.pending.pop(uri, None):
            task.cancel()
        if uri in self.open_uris:
            wait = self.diagnostic_delay if delay is None else delay
            self.pending[uri] = asyncio.ensure_future(self.diagnose(uri, wait))

    def schedule_all(self) -> None:
        for uri in self.open_uris:
            self.schedule(uri)

    async def diagnose(self, uri: str, delay: float) -> None:
        await asyncio.sleep(delay)
        failure: BackendError | None = None
        try:
            # Warm the target cache one job at a time, yielding the worker to requests.
            for target in await self.run(lambda e: e.python_targets(uri), []):
                await self.run(lambda e, t=target: e.target_resolved(uri, t), True)
        except BackendError as exc:
            failure = exc
        version, diagnostics = await self.run(
            lambda e: self._diagnostics(e, uri, failure), (None, [])
        )
        if uri in self.open_uris:
            self.text_document_publish_diagnostics(
                lsp.PublishDiagnosticsParams(uri, diagnostics, version=version)
            )
        if self.pending.get(uri) is asyncio.current_task():
            del self.pending[uri]

    @staticmethod
    def _diagnostics(
        engine: HydraDex, uri: str, failure: BackendError | None
    ) -> tuple[int | None, list[lsp.Diagnostic]]:
        document = engine.document(uri)
        version = document.version if document else None
        if failure is None:
            try:
                return version, engine.diagnostics(uri)
            except BackendError as exc:
                failure = exc
        errors = list(document.errors) if document else []
        return version, [
            *errors,
            lsp.Diagnostic(
                range=lsp.Range(lsp.Position(0, 0), lsp.Position(0, 0)),
                message=str(failure),
                severity=lsp.DiagnosticSeverity.Warning,
                source="hydradex",
                code="backend-unavailable",
            ),
        ]

    # Configuration

    async def apply_settings(self, settings: Settings, *, roots: list[Path] | None = None) -> None:
        if settings == self.settings and (roots is None or roots == self.roots):
            return
        self.settings = settings
        if roots is not None:
            self.roots = roots
        new_roots = self.roots
        await self.run(lambda e: e.reconfigure(roots=new_roots, settings=settings), None)
        self.schedule_all()

    async def pull_settings(self) -> dict[str, Any] | None:
        workspace = self.client_capabilities.workspace
        if not (workspace and workspace.configuration):
            return None
        try:
            result = await self.workspace_configuration_async(
                lsp.ConfigurationParams([lsp.ConfigurationItem(section="hydradex")])
            )
        except Exception as exc:
            log.debug("workspace/configuration failed: %s", exc)
            return None
        return result[0] if result and isinstance(result[0], dict) else None

    def cleanup(self) -> None:
        for task in self.pending.values():
            task.cancel()
        try:
            self.worker.submit(lambda: self.engine and self.engine.shutdown()).result()
        except RuntimeError:
            pass
        self.worker.shutdown(wait=True, cancel_futures=True)


def _roots(params: lsp.InitializeParams) -> list[Path]:
    roots = [p for f in params.workspace_folders or [] if (p := uri_path(f.uri)) is not None]
    if not roots and params.root_uri and (root := uri_path(params.root_uri)):
        roots = [root]
    if not roots and params.root_path:
        roots = [Path(params.root_path).resolve()]
    return roots


def _safe(default: Any):
    """Report backend failures as empty results; the diagnostic explains them."""

    def decorate(handler):
        async def wrapper(params):
            try:
                return await handler(params)
            except BackendError as exc:
                log.warning("%s", exc)
                return default

        wrapper.__name__ = handler.__name__
        return wrapper

    return decorate


def create_server() -> HydraLanguageServer:
    server = HydraLanguageServer()

    @server.feature(lsp.INITIALIZE)
    async def initialize(params: lsp.InitializeParams) -> None:
        try:
            settings, warnings = Settings.from_lsp(params.initialization_options)
        except (ValueError, TypeError) as exc:
            settings, warnings = Settings(), [f"Invalid HydraDex settings: {exc}"]
        server.messages.extend(lsp.ShowMessageParams(lsp.MessageType.Warning, w) for w in warnings)
        server.initial_settings = server.settings = settings
        roots = server.roots = _roots(params)

        def create() -> None:
            server.engine = HydraDex(roots, settings)

        await asyncio.get_running_loop().run_in_executor(server.worker, create)

    @server.feature(lsp.INITIALIZED)
    async def initialized(params: lsp.InitializedParams) -> None:
        for message in server.messages:
            server.warn(message.message, message.type)
        server.messages.clear()
        workspace = server.client_capabilities.workspace
        watched = workspace.did_change_watched_files if workspace else None
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
        document = server.workspace.get_text_document(uri)
        text, version = document.source, document.version
        server.open_uris.add(uri)
        await server.run(lambda e: e.update(uri, text, version), None)
        server.schedule(uri)

    @server.feature(lsp.TEXT_DOCUMENT_DID_OPEN)
    async def opened(params: lsp.DidOpenTextDocumentParams) -> None:
        await sync(params.text_document.uri)

    @server.feature(lsp.TEXT_DOCUMENT_DID_CHANGE)
    async def changed(params: lsp.DidChangeTextDocumentParams) -> None:
        await sync(params.text_document.uri)

    @server.feature(lsp.TEXT_DOCUMENT_DID_CLOSE)
    async def closed(params: lsp.DidCloseTextDocumentParams) -> None:
        uri = params.text_document.uri
        server.open_uris.discard(uri)
        server.schedule(uri)  # Cancels pending diagnostics.
        await server.run(lambda e: e.close(uri), None)
        server.text_document_publish_diagnostics(lsp.PublishDiagnosticsParams(uri, []))

    @server.feature(lsp.TEXT_DOCUMENT_DEFINITION)
    @_safe([])
    async def definitions(params: lsp.DefinitionParams):
        uri, position = params.text_document.uri, params.position
        return await server.run(lambda e: e.definitions(uri, position), [])

    @server.feature(
        lsp.TEXT_DOCUMENT_COMPLETION, lsp.CompletionOptions(trigger_characters=[".", "_"])
    )
    @_safe([])
    async def completions(params: lsp.CompletionParams):
        uri, position = params.text_document.uri, params.position
        return await server.run(lambda e: e.completions(uri, position), [])

    @server.feature(lsp.TEXT_DOCUMENT_HOVER)
    @_safe(None)
    async def hover(params: lsp.HoverParams):
        uri, position = params.text_document.uri, params.position
        return await server.run(lambda e: e.hover(uri, position), None)

    @server.feature(lsp.WORKSPACE_DID_CHANGE_WATCHED_FILES)
    async def files_changed(params: lsp.DidChangeWatchedFilesParams) -> None:
        events = list(params.changes)
        await server.run(lambda e: e.files_changed(events), None)
        server.schedule_all()

    @server.feature(lsp.WORKSPACE_DID_CHANGE_CONFIGURATION)
    async def configuration(params: lsp.DidChangeConfigurationParams) -> None:
        section = settings_section(params.settings)
        if section is None:
            section = await server.pull_settings()
        if section is None:
            return  # Nothing for HydraDex; keep the current configuration.
        try:
            settings, warnings = server.initial_settings.merge(section)
        except (ValueError, TypeError) as exc:
            server.warn(f"Invalid HydraDex settings: {exc}", lsp.MessageType.Error)
            return
        for warning in warnings:
            server.warn(warning)
        await server.apply_settings(settings)

    @server.feature(lsp.WORKSPACE_DID_CHANGE_WORKSPACE_FOLDERS)
    async def folders(params: lsp.DidChangeWorkspaceFoldersParams) -> None:
        removed = {uri_path(folder.uri) for folder in params.event.removed}
        roots = [root for root in server.roots if root not in removed]
        roots += [p for f in params.event.added if (p := uri_path(f.uri)) and p not in roots]
        await server.apply_settings(server.settings, roots=roots)

    @server.command("hydradex.refreshIndex")
    async def refresh(*args: Any) -> None:
        await server.run(lambda e: e.refresh(), None)
        server.schedule_all()

    @server.feature(lsp.SHUTDOWN)
    async def shutdown(*args: Any) -> None:
        for task in server.pending.values():
            task.cancel()
        server.pending.clear()
        await server.run(lambda e: e.shutdown(), None)

    return server
