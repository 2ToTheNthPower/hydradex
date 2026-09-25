import sys
import threading

import pytest
from conftest import fake_backend, logged

from hydradex.lsp_client import BackendError, LspClient


def start(tmp_path, *args, **kwargs):
    requests = []

    def on_request(method, params):
        requests.append(method)
        return [{"answer": 1}]

    client = LspClient(fake_backend("--log", str(tmp_path / "log"), *args), tmp_path, on_request)
    client.request("initialize", {}, timeout=10)
    return client, requests


def test_round_trip_unicode_and_server_requests(tmp_path):
    client, requests = start(tmp_path)
    try:
        text = "import app\napp.Found  # 😀 non-ASCII must be framed by bytes"
        client.notify(
            "textDocument/didOpen", {"textDocument": {"uri": "file:///x.py", "text": text}}
        )
        result = client.request(
            "textDocument/definition", {"textDocument": {"uri": "file:///x.py"}}, timeout=10
        )
        assert result[0]["uri"].endswith("fake_backend.py")
        assert requests == ["workspace/configuration"]
        responses = [m for m in logged(tmp_path / "log") if m.get("id") == "config-1"]
        assert responses == [{"jsonrpc": "2.0", "id": "config-1", "result": [{"answer": 1}]}]
    finally:
        client.close()
    assert client.process.returncode == 0
    assert not client.alive


def test_error_responses_raise(tmp_path):
    client, _ = start(tmp_path, "--error-on", "textDocument/hover")
    try:
        with pytest.raises(BackendError, match="hover failed"):
            client.request("textDocument/hover", {}, timeout=10)
        assert client.request("textDocument/definition", {"textDocument": {"uri": "x"}}, 10) == []
    finally:
        client.close()


def test_timeout_cancels_and_connection_survives(tmp_path):
    client, _ = start(tmp_path, "--delay", "0.5")
    try:
        with pytest.raises(BackendError, match="timed out"):
            client.request("textDocument/definition", {"textDocument": {"uri": "x"}}, 0.05)
        assert client.alive
        assert client.request("textDocument/definition", {"textDocument": {"uri": "x"}}, 10) == []
        methods = [m.get("method") for m in logged(tmp_path / "log")]
        assert "$/cancelRequest" in methods
    finally:
        client.close()


def test_crash_fails_pending_and_future_requests(tmp_path):
    client, _ = start(tmp_path, "--crash-on", "textDocument/definition")
    try:
        with pytest.raises(BackendError, match="exited"):
            client.request("textDocument/definition", {}, timeout=10)
        client.process.wait(timeout=10)
        assert not client.alive
        with pytest.raises(BackendError):
            client.request("textDocument/hover", {}, timeout=10)
    finally:
        client.close()


def test_concurrent_requests_are_matched_by_id(tmp_path):
    client, _ = start(tmp_path)
    results = {}

    def query(i):
        uri = f"file:///{i}.py"
        text = "Found" if i % 2 else "missing"
        client.notify("textDocument/didOpen", {"textDocument": {"uri": uri, "text": text}})
        results[i] = client.request("textDocument/definition", {"textDocument": {"uri": uri}}, 10)

    try:
        threads = [threading.Thread(target=query, args=(i,)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert {i: bool(r) for i, r in results.items()} == {i: bool(i % 2) for i in range(8)}
    finally:
        client.close()


def test_close_kills_unresponsive_process(tmp_path):
    client = LspClient([sys.executable, "-c", "import time; time.sleep(60)"], tmp_path)
    client.close(timeout=0.2)
    assert client.process.returncode not in (None, 0)
    assert not client.alive


def test_missing_command(tmp_path):
    with pytest.raises(BackendError, match="Cannot start"):
        LspClient([str(tmp_path / "missing")], tmp_path)
