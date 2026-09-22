"""Black-box tests using real stdio framing and real Python backend processes."""

import json
import os
import queue
import subprocess
import sys
import threading
import time

import pytest


class Client:
    def __init__(self, root, command=None):
        self.process = subprocess.Popen(
            command or [sys.executable, "-m", "hydradex", "--stdio"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.messages = queue.Queue()
        self.notifications = []
        self.errors = []
        self.counter = 0
        self.reader = threading.Thread(target=self.read, daemon=True)
        self.reader.start()
        self.stderr = threading.Thread(target=self.read_stderr, daemon=True)
        self.stderr.start()
        self.initialized = self.request(
            "initialize",
            {
                "processId": os.getpid(),
                "rootUri": root.as_uri(),
                "capabilities": {
                    "general": {"positionEncodings": ["utf-8", "utf-16"]},
                    "workspace": {"didChangeWatchedFiles": {"dynamicRegistration": True}},
                },
            },
        )
        self.notify("initialized", {})

    def read(self):
        try:
            while True:
                headers = {}
                while line := self.process.stdout.readline():
                    if line == b"\r\n":
                        break
                    key, value = line.decode().split(":", 1)
                    headers[key.lower()] = value.strip()
                if not headers:
                    return
                length = int(headers["content-length"])
                payload = self.process.stdout.read(length)
                self.messages.put(json.loads(payload))
        except Exception as exc:
            self.messages.put({"read_error": str(exc)})

    def read_stderr(self):
        for line in self.process.stderr:
            self.errors.append(line.decode(errors="replace"))

    def send(self, message):
        body = json.dumps({"jsonrpc": "2.0", **message}).encode()
        self.process.stdin.write(f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
        self.process.stdin.flush()

    def notify(self, method, params):
        self.send({"method": method, "params": params})

    def wait(self, predicate, timeout=20):
        deadline = time.monotonic() + timeout
        while True:
            try:
                message = self.messages.get(timeout=max(0, deadline - time.monotonic()))
            except queue.Empty:
                pytest.fail("LSP timed out: " + "".join(self.errors))
            assert "read_error" not in message, message
            if "method" in message and "id" in message:
                self.send({"id": message["id"], "result": None})
            if predicate(message):
                return message
            self.notifications.append(message)

    def request(self, method, params):
        self.counter += 1
        request_id = self.counter
        self.send({"id": request_id, "method": method, "params": params})
        response = self.wait(lambda message: message.get("id") == request_id)
        assert "error" not in response, response
        return response.get("result")

    def diagnostic(self, uri, version=None):
        def matches(message):
            params = message.get("params", {})
            return (
                message.get("method") == "textDocument/publishDiagnostics"
                and params["uri"] == uri
                and (version is None or params.get("version") == version)
            )

        for i, message in enumerate(self.notifications):
            if matches(message):
                return self.notifications.pop(i)["params"]["diagnostics"]
        return self.wait(matches)["params"]["diagnostics"]

    def close(self):
        try:
            if self.process.poll() is None:
                self.request("shutdown", None)
                self.notify("exit", None)
                self.process.wait(timeout=10)
                assert self.process.returncode == 0, "".join(self.errors)
        finally:
            if self.process.poll() is None:
                self.process.kill()
                self.process.wait(timeout=5)
            self.reader.join(timeout=2)
            self.stderr.join(timeout=2)
            self.process.stdin.close()
            self.process.stdout.close()
            self.process.stderr.close()


@pytest.fixture
def client(project):
    root, write = project
    write(
        "models.py",
        "class Model:\n    def __init__(self, width: int, *, bias: bool = True): pass\n",
    )
    instance = Client(root)
    try:
        yield instance, root, write
    finally:
        instance.close()


def test_stdio_features_and_incremental_edits(client):
    rpc, root, write = client
    caps = rpc.initialized["capabilities"]
    assert caps["positionEncoding"] == "utf-16"
    assert caps["textDocumentSync"]["change"] == 2
    assert caps["definitionProvider"]
    assert caps["completionProvider"]
    assert caps["hoverProvider"]
    uri = (root / "config.yaml").as_uri()
    text = "model:\n  _target_: models.Model\n  \nref: ${model}"
    rpc.notify(
        "textDocument/didOpen",
        {
            "textDocument": {
                "uri": uri,
                "languageId": "yaml",
                "version": 1,
                "text": text,
            }
        },
    )
    assert rpc.diagnostic(uri, 1) == []
    params = {"textDocument": {"uri": uri}, "position": {"line": 1, "character": 20}}
    locations = rpc.request("textDocument/definition", params)
    assert locations[0]["uri"] == (root / "models.py").as_uri()
    assert "Model" in rpc.request("textDocument/hover", params)["contents"]["value"]
    params["position"] = {"line": 2, "character": 2}
    completions = rpc.request("textDocument/completion", params)
    assert {item["label"] for item in completions} == {"width", "bias"}
    rpc.notify(
        "textDocument/didChange",
        {
            "textDocument": {"uri": uri, "version": 2},
            "contentChanges": [
                {
                    "range": {
                        "start": {"line": 1, "character": 19},
                        "end": {"line": 1, "character": 24},
                    },
                    "text": "Missing",
                }
            ],
        },
    )
    assert rpc.diagnostic(uri, 2)[0]["code"] == "unresolved-target"
    rpc.notify(
        "textDocument/didChange",
        {
            "textDocument": {"uri": uri, "version": 3},
            "contentChanges": [{"text": text}],
        },
    )
    assert rpc.diagnostic(uri, 3) == []
    rpc.notify("textDocument/didClose", {"textDocument": {"uri": uri}})
    assert rpc.diagnostic(uri) == []
    assert not list(root.glob("__hydradex*"))


def test_watched_files_settings_refresh_and_folders(client):
    rpc, root, write = client
    uri = (root / "config.yaml").as_uri()
    rpc.notify(
        "textDocument/didOpen",
        {
            "textDocument": {
                "uri": uri,
                "languageId": "yaml",
                "version": 1,
                "text": "ref: ${name}",
            }
        },
    )
    assert rpc.diagnostic(uri, 1) == []
    params = {"textDocument": {"uri": uri}, "position": {"line": 0, "character": 9}}
    assert not rpc.request("textDocument/definition", params)
    target = write("data.yaml", "name: value")
    rpc.notify(
        "workspace/didChangeWatchedFiles", {"changes": [{"uri": target.as_uri(), "type": 1}]}
    )
    assert rpc.diagnostic(uri, 1) == []
    assert rpc.request("textDocument/definition", params)[0]["uri"] == target.as_uri()
    target.unlink()
    rpc.notify(
        "workspace/didChangeWatchedFiles", {"changes": [{"uri": target.as_uri(), "type": 3}]}
    )
    assert rpc.diagnostic(uri, 1) == []
    assert not rpc.request("textDocument/definition", params)
    target = write("new.yaml", "name: new")
    rpc.request("workspace/executeCommand", {"command": "hydradex.refreshIndex"})
    assert rpc.request("textDocument/definition", params)[0]["uri"] == target.as_uri()
    rpc.notify(
        "workspace/didChangeConfiguration",
        {
            "settings": {
                "hydradex": {
                    "excludePatterns": ["new.yaml"],
                }
            }
        },
    )
    assert rpc.diagnostic(uri, 1) == []
    assert not rpc.request("textDocument/definition", params)
    other = write("other/data.yaml", "name: other")
    rpc.notify(
        "workspace/didChangeWorkspaceFolders",
        {
            "event": {
                "added": [{"uri": other.parent.as_uri(), "name": "other"}],
                "removed": [],
            }
        },
    )
    # A request is a queue barrier for the serialized library worker.
    assert not rpc.request("textDocument/definition", params)


def test_utf16_diagnostics_and_cli(client):
    rpc, root, _ = client
    uri = (root / "unicode.yaml").as_uri()
    text = "😀: {_target_: models.Missing}"
    rpc.notify(
        "textDocument/didOpen",
        {
            "textDocument": {
                "uri": uri,
                "languageId": "yaml",
                "version": 1,
                "text": text,
            }
        },
    )
    diagnostics = rpc.diagnostic(uri, 1)
    assert diagnostics[0]["range"]["start"]["character"] == text.index("models") + 1
    version = subprocess.run(
        [sys.executable, "-m", "hydradex", "--version"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert version.stdout.strip() == "hydradex 0.2.0"
    assert not version.stderr


def test_python_file_changes_and_invalid_configuration(client):
    rpc, root, write = client
    uri = (root / "config.yaml").as_uri()
    rpc.notify(
        "textDocument/didOpen",
        {
            "textDocument": {
                "uri": uri,
                "languageId": "yaml",
                "version": 1,
                "text": "_target_: models.Model",
            }
        },
    )
    assert rpc.diagnostic(uri, 1) == []
    write("models.py", "class Renamed: pass\n")
    rpc.notify(
        "workspace/didChangeWatchedFiles",
        {
            "changes": [
                {"uri": (root / "models.py").as_uri(), "type": 2},
            ]
        },
    )
    assert rpc.diagnostic(uri, 1)[0]["code"] == "unresolved-target"
    rpc.notify(
        "workspace/didChangeConfiguration",
        {
            "settings": {
                "hydradex": {"matchFilter": "invalid"},
            }
        },
    )
    message = rpc.wait(lambda m: m.get("method") == "window/showMessage")
    assert "matchFilter" in message["params"]["message"]
    rpc.notify(
        "textDocument/didChange",
        {
            "textDocument": {"uri": uri, "version": 2},
            "contentChanges": [{"text": "_target_: models.Renamed"}],
        },
    )
    assert rpc.diagnostic(uri, 2) == []
    rpc.notify("textDocument/didSave", {"textDocument": {"uri": uri}})
    assert rpc.diagnostic(uri, 2) == []


def test_backend_failure_diagnostic_and_recovery(client):
    rpc, root, _ = client
    rpc.notify(
        "workspace/didChangeConfiguration",
        {
            "settings": {
                "hydradex": {
                    "backendCommand": [str(root / "missing-backend")],
                }
            }
        },
    )
    uri = (root / "config.yaml").as_uri()
    rpc.notify(
        "textDocument/didOpen",
        {
            "textDocument": {
                "uri": uri,
                "languageId": "yaml",
                "version": 1,
                "text": "_target_: models.Model",
            }
        },
    )
    diagnostic = rpc.diagnostic(uri, 1)
    assert diagnostic[0]["code"] == "backend-unavailable"
    rpc.notify("workspace/didChangeConfiguration", {"settings": {"hydradex": {}}})
    assert rpc.diagnostic(uri, 1) == []


def test_inherited_target_completion_over_stdio(client):
    rpc, root, write = client
    base = write("base.yaml", "model:\n  _target_: models.Model\n  width: 4\n")
    rpc.notify("workspace/didChangeWatchedFiles", {"changes": [{"uri": base.as_uri(), "type": 1}]})
    uri = (root / "experiment.yaml").as_uri()
    rpc.notify(
        "textDocument/didOpen",
        {
            "textDocument": {
                "uri": uri,
                "languageId": "yaml",
                "version": 1,
                "text": "defaults: [base]\nmodel:\n  ",
            }
        },
    )
    assert rpc.diagnostic(uri, 1) == []
    response = rpc.request(
        "textDocument/completion",
        {
            "textDocument": {"uri": uri},
            "position": {"line": 2, "character": 2},
        },
    )
    assert {"width", "bias"} <= {item["label"] for item in response}
