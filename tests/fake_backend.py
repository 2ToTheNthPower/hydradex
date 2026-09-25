"""A scriptable stand-in for ty, used to test failure modes deterministically.

Usage: python fake_backend.py [--hang-initialize] [--delay SECONDS] [--crash-on METHOD]
       [--error-on METHOD] [--log FILE]

Definitions resolve to this file for any snippet whose last line contains "Found".
Every received message is appended to --log as a JSON line.
"""

import argparse
import json
import sys
import time
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--hang-initialize", action="store_true")
parser.add_argument("--delay", type=float, default=0.0)
parser.add_argument("--crash-on")
parser.add_argument("--error-on")
parser.add_argument("--log")
args = parser.parse_args()
documents: dict[str, str] = {}


def read():
    length = None
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            sys.exit(0)
        line = line.strip()
        if not line:
            break
        name, _, value = line.decode().partition(":")
        if name.lower() == "content-length":
            length = int(value)
    return json.loads(sys.stdin.buffer.read(length))


def send(message):
    body = json.dumps({"jsonrpc": "2.0", **message}).encode()
    sys.stdout.buffer.write(f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
    sys.stdout.buffer.flush()


def respond(message):
    method = message["method"]
    params = message.get("params") or {}
    if method == "initialize":
        if args.hang_initialize:
            time.sleep(60)
        # Exercise server-initiated requests before answering.
        send({"id": "config-1", "method": "workspace/configuration", "params": {"items": [{}]}})
        return {"capabilities": {"definitionProvider": True}}
    if method == "shutdown":
        return None
    if method == args.error_on:
        raise ValueError(f"{method} failed")
    time.sleep(args.delay)
    if method == "textDocument/definition":
        text = documents.get(params["textDocument"]["uri"], "")
        if "Found" in text.split("\n")[-1]:
            span = {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 1}}
            return [{"uri": Path(__file__).resolve().as_uri(), "range": span}]
        return []
    if method == "textDocument/hover":
        return {"contents": {"kind": "markdown", "value": "fake hover"}}
    return None


while True:
    message = read()
    if args.log:
        with open(args.log, "a", encoding="utf-8") as log:
            log.write(json.dumps(message) + "\n")
    method = message.get("method")
    if method is not None and method == args.crash_on:
        sys.exit(3)
    if method == "exit":
        sys.exit(0)
    if method == "textDocument/didOpen":
        item = message["params"]["textDocument"]
        documents[item["uri"]] = item["text"]
    elif method == "textDocument/didChange":
        documents[message["params"]["textDocument"]["uri"]] = message["params"]["contentChanges"][
            -1
        ]["text"]
    if method is None or "id" not in message:
        continue
    try:
        send({"id": message["id"], "result": respond(message)})
    except ValueError as exc:
        send({"id": message["id"], "error": {"code": -32000, "message": str(exc)}})
