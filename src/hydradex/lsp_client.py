"""A minimal synchronous JSON-RPC client for a language server over stdio.

One reader thread dispatches responses to waiting callers and answers requests
initiated by the server. Callers block with a deadline, so a hung backend can
never block the caller indefinitely.
"""

from __future__ import annotations

import itertools
import json
import logging
import subprocess
import threading
from collections.abc import Callable, Sequence
from concurrent.futures import Future
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

RequestHandler = Callable[[str, Any], Any]


class BackendError(RuntimeError):
    """The Python backend failed, exited, or exceeded its request deadline."""


class LspClient:
    def __init__(
        self,
        command: Sequence[str],
        cwd: Path,
        on_request: RequestHandler | None = None,
        on_notification: Callable[[str, Any], None] | None = None,
    ):
        try:
            self.process = subprocess.Popen(
                list(command),
                cwd=cwd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except OSError as exc:
            raise BackendError(f"Cannot start Python backend {command[0]!r}: {exc}") from exc
        self._on_request = on_request or (lambda method, params: None)
        self._on_notification = on_notification or (lambda method, params: None)
        self._ids = itertools.count(1)
        self._pending: dict[int, Future[Any]] = {}
        self._pending_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._exited = False
        self._reader = threading.Thread(target=self._read, name="hydradex-lsp-reader", daemon=True)
        self._stderr = threading.Thread(target=self._drain, name="hydradex-lsp-stderr", daemon=True)
        self._reader.start()
        self._stderr.start()

    @property
    def alive(self) -> bool:
        return not self._exited and self.process.poll() is None

    def request(self, method: str, params: Any, timeout: float) -> Any:
        future: Future[Any] = Future()
        with self._pending_lock:
            if self._exited:
                raise BackendError("Python backend exited")
            request_id = next(self._ids)
            self._pending[request_id] = future
        self._send({"id": request_id, "method": method, "params": params})
        try:
            return future.result(timeout=timeout)
        except TimeoutError:
            with self._pending_lock:
                self._pending.pop(request_id, None)
            try:
                self.notify("$/cancelRequest", {"id": request_id})
            except BackendError:
                pass
            raise BackendError(f"Python backend request {method} timed out") from None

    def notify(self, method: str, params: Any) -> None:
        self._send({"method": method, "params": params})

    def close(self, timeout: float = 2.0) -> None:
        try:
            if self.alive:
                self.request("shutdown", None, timeout)
                self.notify("exit", None)
                self.process.wait(timeout=timeout)
        except (BackendError, subprocess.TimeoutExpired):
            pass
        finally:
            if self.process.poll() is None:
                self.process.kill()
                self.process.wait()
            for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
                try:
                    if stream is not None:
                        stream.close()
                except OSError:
                    pass
            for thread in (self._reader, self._stderr):
                if thread is not threading.current_thread():
                    thread.join(timeout=timeout)
            self._fail_pending()

    def _send(self, message: dict[str, Any]) -> None:
        body = json.dumps({"jsonrpc": "2.0", **message}, ensure_ascii=False).encode("utf-8")
        header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
        stdin = self.process.stdin
        try:
            with self._write_lock:
                assert stdin is not None
                stdin.write(header + body)
                stdin.flush()
        except (OSError, ValueError) as exc:
            raise BackendError(f"Python backend is not accepting input: {exc}") from exc

    def _read(self) -> None:
        stream = self.process.stdout
        assert stream is not None
        try:
            while True:
                length = None
                while True:
                    line = stream.readline()
                    if not line:
                        return
                    line = line.strip()
                    if not line:
                        if length is not None:
                            break
                        continue
                    name, _, value = line.decode("ascii", errors="replace").partition(":")
                    if name.strip().lower() == "content-length":
                        length = int(value.strip())
                body = stream.read(length)
                if len(body) < length:
                    return
                self._dispatch(json.loads(body))
        except Exception:
            log.exception("Python backend connection failed")
        finally:
            self._fail_pending()

    def _dispatch(self, message: dict[str, Any]) -> None:
        method = message.get("method")
        if method is None:
            with self._pending_lock:
                future = self._pending.pop(message.get("id"), None)  # type: ignore[arg-type]
            if future is None:
                return
            if "error" in message:
                error = message["error"] or {}
                future.set_exception(BackendError(f"Python backend: {error.get('message')}"))
            else:
                future.set_result(message.get("result"))
        elif "id" in message:
            try:
                response = {"result": self._on_request(method, message.get("params"))}
            except Exception as exc:
                response = {"error": {"code": -32603, "message": str(exc)}}
            try:
                self._send({"id": message["id"], **response})
            except BackendError:
                pass
        else:
            self._on_notification(method, message.get("params"))

    def _drain(self) -> None:
        stream = self.process.stderr
        assert stream is not None
        try:
            for line in stream:
                log.debug("Python backend: %s", line.decode(errors="replace").rstrip())
        except (OSError, ValueError):
            pass

    def _fail_pending(self) -> None:
        with self._pending_lock:
            self._exited = True
            pending, self._pending = self._pending, {}
        for future in pending.values():
            if not future.done():
                future.set_exception(BackendError("Python backend exited"))
