"""Black-box tests over real stdio framing, with real ty or the scripted fake backend."""

import json
import os
import queue
import subprocess
import sys
import threading
import time

import pytest
from conftest import fake_backend, logged

MODELS = "class Model:\n    def __init__(self, width: int, *, bias: bool = True): pass\n"


class Client:
    def __init__(self, root, *, options=None, capabilities=None, configuration=None):
        self.process = subprocess.Popen(
            [sys.executable, "-m", "hydradex", "--stdio"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.messages = queue.Queue()
        self.notifications = []
        self.errors = []
        self.counter = 0
        # Results for server-to-client requests, by method.
        self.configuration = configuration
        threading.Thread(target=self.read, daemon=True).start()
        threading.Thread(target=self.read_stderr, daemon=True).start()
        self.initialized = self.request(
            "initialize",
            {
                "processId": os.getpid(),
                "rootUri": root.as_uri(),
                "initializationOptions": options,
                "capabilities": capabilities
                or {
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
                payload = self.process.stdout.read(int(headers["content-length"]))
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

    def wait(self, predicate, timeout=30):
        deadline = time.monotonic() + timeout
        while True:
            try:
                message = self.messages.get(timeout=max(0, deadline - time.monotonic()))
            except queue.Empty:
                pytest.fail("LSP timed out: " + "".join(self.errors))
            assert "read_error" not in message, message
            if "method" in message and "id" in message:
                result = None
                if message["method"] == "workspace/configuration":
                    result = [self.configuration for _ in message["params"]["items"]]
                self.send({"id": message["id"], "result": result})
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

    def published(self, uri, version=None):
        def matches(message):
            params = message.get("params", {})
            return (
                message.get("method") == "textDocument/publishDiagnostics"
                and params["uri"] == uri
                and (version is None or params.get("version") == version)
            )

        for i, message in enumerate(self.notifications):
            if matches(message):
                return self.notifications.pop(i)["params"]
        return self.wait(matches)["params"]

    def diagnostic(self, uri, version=None):
        return self.published(uri, version)["diagnostics"]

    def open(self, uri, text, version=1):
        self.notify(
            "textDocument/didOpen",
            {"textDocument": {"uri": uri, "languageId": "yaml", "version": version, "text": text}},
        )

    def change(self, uri, version, text):
        self.notify(
            "textDocument/didChange",
            {"textDocument": {"uri": uri, "version": version}, "contentChanges": [{"text": text}]},
        )

    def configure(self, settings):
        self.notify("workspace/didChangeConfiguration", {"settings": settings})

    def at(self, method, uri, line, character):
        params = {"textDocument": {"uri": uri}, "position": {"line": line, "character": character}}
        return self.request(method, params)

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
            for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
                stream.close()


@pytest.fixture
def connect(project):
    root, write = project
    write("models.py", MODELS)
    clients = []

    def start(**kwargs):
        clients.append(Client(root, **kwargs))
        return clients[-1]

    try:
        yield start, root, write
    finally:
        for client in clients:
            client.close()


def definition_uris(rpc, uri, line, character):
    return [loc["uri"] for loc in rpc.at("textDocument/definition", uri, line, character) or []]


def test_stdio_features_and_incremental_edits(connect):
    start, root, _ = connect
    rpc = start()
    caps = rpc.initialized["capabilities"]
    assert caps["positionEncoding"] == "utf-16"
    assert caps["textDocumentSync"]["change"] == 2
    assert caps["definitionProvider"] and caps["hoverProvider"]
    assert set(caps["completionProvider"]["triggerCharacters"]) == {".", "_"}
    uri = (root / "config.yaml").as_uri()
    text = "model:\n  _target_: models.Model\n  \nref: ${model}"
    rpc.open(uri, text)
    assert rpc.diagnostic(uri, 1) == []
    assert definition_uris(rpc, uri, 1, 20) == [(root / "models.py").as_uri()]
    assert "Model" in rpc.at("textDocument/hover", uri, 1, 20)["contents"]["value"]
    completions = rpc.at("textDocument/completion", uri, 2, 2)
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
    rpc.change(uri, 3, text)
    assert rpc.diagnostic(uri, 3) == []
    rpc.notify("textDocument/didClose", {"textDocument": {"uri": uri}})
    assert rpc.published(uri) == {"uri": uri, "diagnostics": []}
    assert not list(root.glob("__hydradex*"))


def test_watched_files_and_refresh(connect):
    start, root, write = connect
    rpc = start()
    registration = rpc.wait(lambda m: m.get("method") == "client/registerCapability")
    [watcher] = registration["params"]["registrations"][0]["registerOptions"]["watchers"]
    assert watcher["globPattern"] == "**/*.{yaml,yml,py,pyi}"
    uri = (root / "config.yaml").as_uri()
    rpc.open(uri, "ref: ${name}")
    assert rpc.diagnostic(uri, 1) == []
    assert not definition_uris(rpc, uri, 0, 9)
    target = write("data.yaml", "name: value")
    rpc.notify(
        "workspace/didChangeWatchedFiles", {"changes": [{"uri": target.as_uri(), "type": 1}]}
    )
    assert rpc.diagnostic(uri, 1) == []
    assert definition_uris(rpc, uri, 0, 9) == [target.as_uri()]
    target.unlink()
    rpc.notify(
        "workspace/didChangeWatchedFiles", {"changes": [{"uri": target.as_uri(), "type": 3}]}
    )
    assert rpc.diagnostic(uri, 1) == []
    assert not definition_uris(rpc, uri, 0, 9)
    target = write("new.yaml", "name: new")
    rpc.request("workspace/executeCommand", {"command": "hydradex.refreshIndex"})
    assert definition_uris(rpc, uri, 0, 9) == [target.as_uri()]


def test_python_file_changes_reach_ty(connect):
    start, root, write = connect
    rpc = start()
    uri = (root / "config.yaml").as_uri()
    rpc.open(uri, "_target_: models.Model")
    assert rpc.diagnostic(uri, 1) == []
    write("models.py", "class Renamed: pass\n")
    changes = [{"uri": (root / "models.py").as_uri(), "type": 2}]
    rpc.notify("workspace/didChangeWatchedFiles", {"changes": changes})
    assert rpc.diagnostic(uri, 1)[0]["code"] == "unresolved-target"
    rpc.change(uri, 2, "_target_: models.Renamed")
    assert rpc.diagnostic(uri, 2) == []
    rpc.notify("textDocument/didSave", {"textDocument": {"uri": uri}})
    assert definition_uris(rpc, uri, 0, 18) == [(root / "models.py").as_uri()]


def test_configuration_changes_merge_with_initialization_options(connect):
    start, root, write = connect
    rpc = start(options={"excludePatterns": ["hidden/"]})
    uri = (root / "config.yaml").as_uri()
    write("hidden/data.yaml", "name: hidden")
    rpc.open(uri, "ref: ${name}")
    assert rpc.diagnostic(uri, 1) == []
    assert not definition_uris(rpc, uri, 0, 9)
    # Payloads without a hydradex section must not reset the initialization options.
    for payload in ({"yaml": {"schemas": {}}}, None, {}):
        rpc.configure(payload)
        assert not definition_uris(rpc, uri, 0, 9)
    # A hydradex section overrides only the keys it contains.
    rpc.configure({"hydradex": {"matchFilter": "all"}})
    assert rpc.diagnostic(uri, 1) == []
    assert not definition_uris(rpc, uri, 0, 9)
    rpc.configure({"hydradex": {"excludePatterns": []}})
    assert rpc.diagnostic(uri, 1) == []
    assert definition_uris(rpc, uri, 0, 9) == [(root / "hidden/data.yaml").as_uri()]


def test_unchanged_configuration_does_not_rebuild(connect):
    start, root, write = connect
    rpc = start(options={"hydradex": {"matchFilter": "all"}})
    uri = (root / "config.yaml").as_uri()
    rpc.open(uri, "ref: ${name}")
    assert rpc.diagnostic(uri, 1) == []
    # Written without a watcher notification: only a rebuild would index it.
    target = write("data.yaml", "name: value")
    rpc.configure({"hydradex": {"matchFilter": "all"}})
    assert not definition_uris(rpc, uri, 0, 9)
    rpc.configure({"hydradex": {"matchFilter": "top matches only"}})
    assert rpc.diagnostic(uri, 1) == []
    assert definition_uris(rpc, uri, 0, 9) == [target.as_uri()]


def test_pulled_configuration_and_setting_warnings(connect):
    start, root, write = connect
    capabilities = {"workspace": {"configuration": True}}
    write("hidden/data.yaml", "name: hidden")
    rpc = start(
        options={"configRoot": ["typo"]},
        capabilities=capabilities,
        configuration={"excludePatterns": ["hidden/"]},
    )
    warning = rpc.wait(lambda m: m.get("method") == "window/showMessage")
    assert "configRoot" in warning["params"]["message"]
    uri = (root / "config.yaml").as_uri()
    rpc.open(uri, "ref: ${name}")
    assert rpc.diagnostic(uri, 1) == []
    assert definition_uris(rpc, uri, 0, 9)
    rpc.configure(None)  # Pull-style clients send no payload.
    assert rpc.diagnostic(uri, 1) == []
    assert not definition_uris(rpc, uri, 0, 9)
    rpc.configure({"hydradex": {"matchFilter": "invalid"}})
    message = rpc.wait(lambda m: m.get("method") == "window/showMessage")
    assert "matchFilter" in message["params"]["message"]
    assert message["params"]["type"] == 1


def test_workspace_folder_changes_republish(connect):
    start, root, write = connect
    rpc = start()
    uri = (root / "config.yaml").as_uri()
    rpc.open(uri, "ref: ${name}")
    assert rpc.diagnostic(uri, 1) == []
    other = write("other/data.yaml", "name: other").parent
    event = {"added": [{"uri": other.as_uri(), "name": "other"}], "removed": []}
    rpc.notify("workspace/didChangeWorkspaceFolders", {"event": event})
    assert rpc.diagnostic(uri, 1) == []
    # The nested folder now owns its files, so isolation hides them from the root.
    assert not definition_uris(rpc, uri, 0, 9)
    event = {"added": [], "removed": [{"uri": other.as_uri(), "name": "other"}]}
    rpc.notify("workspace/didChangeWorkspaceFolders", {"event": event})
    assert rpc.diagnostic(uri, 1) == []
    assert definition_uris(rpc, uri, 0, 9) == [(other / "data.yaml").as_uri()]


def test_utf16_diagnostics_and_cli(connect):
    start, root, _ = connect
    rpc = start()
    uri = (root / "unicode.yaml").as_uri()
    text = "😀: {_target_: models.Missing}"
    rpc.open(uri, text)
    diagnostics = rpc.diagnostic(uri, 1)
    assert diagnostics[0]["range"]["start"]["character"] == text.index("models") + 1
    version = subprocess.run(
        [sys.executable, "-m", "hydradex", "--version"], capture_output=True, text=True, check=True
    )
    from hydradex import __version__

    assert version.stdout.strip() == f"hydradex {__version__}"
    assert not version.stderr


def test_backend_failure_is_a_diagnostic_not_a_request_error(connect):
    start, root, _ = connect
    rpc = start(options={"backendCommand": [str(root / "missing-backend")]})
    uri = (root / "config.yaml").as_uri()
    rpc.open(uri, "_target_: models.Model")
    [diagnostic] = rpc.diagnostic(uri, 1)
    assert diagnostic["code"] == "backend-unavailable"
    assert "missing-backend" in diagnostic["message"]
    assert rpc.at("textDocument/definition", uri, 0, 15) == []
    assert rpc.at("textDocument/hover", uri, 0, 15) is None
    assert rpc.at("textDocument/completion", uri, 0, 15) == []
    rpc.configure({"hydradex": {"backendCommand": None}})
    assert rpc.diagnostic(uri, 1) == []


def test_syntax_errors_need_no_backend(connect):
    start, root, _ = connect
    rpc = start(options={"backendCommand": [str(root / "missing-backend")]})
    uri = (root / "config.yaml").as_uri()
    rpc.open(uri, "_target_: models.Model\nbroken: [")
    assert [d["code"] for d in rpc.diagnostic(uri, 1)] == ["yaml-syntax"]


def test_diagnostics_are_debounced(connect):
    start, root, _ = connect
    rpc = start()
    uri = (root / "config.yaml").as_uri()
    rpc.open(uri, "a: 1")
    assert rpc.diagnostic(uri, 1) == []
    for version in range(2, 7):
        rpc.change(uri, version, f"_target_: models.Missing{version}")
    published = rpc.published(uri)
    assert published["version"] == 6
    assert "Missing6" in published["diagnostics"][0]["message"]
    rpc.request("workspace/executeCommand", {"command": "hydradex.refreshIndex"})
    rpc.published(uri, 6)
    earlier = [m for m in rpc.notifications if m.get("method") == "textDocument/publishDiagnostics"]
    assert earlier == []


def test_requests_follow_preceding_edits(connect):
    start, root, _ = connect
    rpc = start()
    uri = (root / "config.yaml").as_uri()
    rpc.open(uri, "a: 1")
    rpc.change(uri, 2, "model:\n  _target_: models.Model\n  wi")
    labels = {item["label"] for item in rpc.at("textDocument/completion", uri, 2, 4)}
    assert labels == {"width"}


def test_requests_are_answered_while_slow_diagnostics_run(connect):
    start, root, write = connect
    write("base.yaml", "training:\n  epochs: 5\n")
    rpc = start(options={"backendCommand": list(fake_backend("--delay", "0.5"))})
    uri = (root / "config.yaml").as_uri()
    count = 8  # Resolving all targets takes at least 4.5 seconds, module probe included.
    targets = "\n".join(f"m{i}: {{_target_: pkg.Found{i}}}" for i in range(count))
    rpc.open(uri, f"defaults: [base]\n{targets}\ntraining:\n  ep")
    time.sleep(0.5)  # Diagnostics are now resolving targets.
    started = time.monotonic()
    completions = rpc.at("textDocument/completion", uri, count + 2, 4)
    elapsed = time.monotonic() - started
    assert [item["label"] for item in completions] == ["epochs"]
    # Answered between target jobs, not after all of them.
    assert not any(m.get("method") == "textDocument/publishDiagnostics" for m in rpc.notifications)
    assert elapsed < 3.0, elapsed
    assert rpc.diagnostic(uri, 1) == []


def test_inherited_target_completion_over_stdio(connect):
    start, root, write = connect
    rpc = start()
    base = write("base.yaml", "model:\n  _target_: models.Model\n  width: 4\n")
    rpc.notify("workspace/didChangeWatchedFiles", {"changes": [{"uri": base.as_uri(), "type": 1}]})
    uri = (root / "experiment.yaml").as_uri()
    rpc.open(uri, "defaults: [base]\nmodel:\n  ")
    assert rpc.diagnostic(uri, 1) == []
    labels = {item["label"] for item in rpc.at("textDocument/completion", uri, 2, 2)}
    assert {"width", "bias"} <= labels


@pytest.mark.parametrize("orderly", [True, False])
def test_exit_stops_backends(connect, orderly):
    start, root, _ = connect
    log = root / "backend.log"
    rpc = start(options={"backendCommand": list(fake_backend("--log", str(log)))})
    uri = (root / "config.yaml").as_uri()
    rpc.open(uri, "_target_: pkg.Found")
    assert rpc.diagnostic(uri, 1) == []
    if orderly:
        rpc.request("shutdown", None)
    rpc.notify("exit", None)
    rpc.process.wait(timeout=15)
    methods = [m.get("method") for m in logged(log)]
    assert methods[-2:] == ["shutdown", "exit"]
