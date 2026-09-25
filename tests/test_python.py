import sys

import pytest
from conftest import cursor, fake_settings, logged
from lsprotocol import types as lsp

from hydradex import BackendError, HydraDex, Settings
from hydradex.python import PythonAnalysis, valid_target

# Real ty analysis.


@pytest.mark.parametrize(
    "target",
    [
        "app.models.Model",
        "app.Exported",
        "app.models.factory",
        "app.models.Data",
        "app.models.Model.create",
        "builtins.dict",
    ],
)
def test_definitions(python_project, target):
    root, engine = python_project
    uri = (root / "test.yaml").as_uri()
    engine.update(uri, f"_target_: {target}\n")
    locations = engine.definitions(uri, lsp.Position(0, 15))
    assert locations
    assert all(location.uri.endswith((".py", ".pyi")) for location in locations)
    assert not engine.diagnostics(uri)
    assert not list(root.glob("__hydradex*"))
    assert not list(root.rglob("*.executed"))


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        ("app.models.Model", {"width", "bias"}),
        ("app.Exported", {"width", "bias"}),
        ("app.models.Data", {"name", "count"}),
        ("app.models.factory", {"name", "enabled"}),
        ("app.models.Model.create", {"width", "device"}),
        ("app.models.positional", {"flag"}),
        ("app.models.positional_only", set()),
        ("app.models.variadic_only", set()),
        ("app.models.overloaded", {"value", "count", "suffix"}),
    ],
)
def test_parameters(python_project, target, expected):
    root, engine = python_project
    uri = (root / "test.yaml").as_uri()
    engine.update(uri, f"object:\n  _target_: {target}\n  ")
    items = engine.completions(uri, lsp.Position(2, 2))
    assert {item.label for item in items} == expected
    assert all(item.text_edit.new_text == item.label + ": " for item in items)


@pytest.mark.parametrize("quote", ["", "'", '"'])
def test_target_completion_quotes_and_mid_token(python_project, quote):
    root, engine = python_project
    uri = (root / "test.yaml").as_uri()
    prefix = f"_target_: {quote}app.models.M"
    text = prefix + "issing" + quote
    engine.update(uri, text)
    items = engine.completions(uri, lsp.Position(0, len(prefix)))
    item = next(item for item in items if item.label == "Model")
    edit = item.text_edit
    updated = text[: edit.range.start.character] + edit.new_text + text[edit.range.end.character :]
    assert updated == f"_target_: {quote}app.models.Model{quote}"
    assert item.kind == lsp.CompletionItemKind.Class


@pytest.mark.parametrize("prefix", ["", "app.", "app.models."])
def test_module_completion_levels(python_project, prefix):
    root, engine = python_project
    uri = (root / "test.yaml").as_uri()
    engine.update(uri, f"_target_: {prefix}")
    items = engine.completions(uri, lsp.Position(0, 10 + len(prefix)))
    assert items
    if prefix == "app.models.":
        assert {"Model", "Data", "factory"} <= {item.label for item in items}


def test_target_completion_in_a_list_item(python_project):
    root, engine = python_project
    uri = (root / "test.yaml").as_uri()
    text, position = cursor("layers:\n  - _target_: app.models.fac|")
    engine.update(uri, text)
    assert [item.label for item in engine.completions(uri, position)] == ["factory"]


def test_parameter_prefix_and_siblings(python_project):
    root, engine = python_project
    uri = (root / "test.yaml").as_uri()
    engine.update(uri, "_target_: app.models.Model\nwidth: 5\nbi")
    assert [i.label for i in engine.completions(uri, lsp.Position(2, 2))] == ["bias"]
    engine.update(uri, "_target_: app.models.Model\nwidth: 5\nbi: false")
    assert engine.completions(uri, lsp.Position(2, 2))[0].text_edit.new_text == "bias"


def test_hover(python_project):
    root, engine = python_project
    uri = (root / "test.yaml").as_uri()
    engine.update(uri, "_target_: app.models.Model")
    hover = engine.hover(uri, lsp.Position(0, 15))
    assert hover is not None
    assert "Model" in hover.contents.value
    assert hover.range.start.character == 10
    assert engine.hover(uri, lsp.Position(0, 0)) is None
    engine.update(uri, "_target_: app.models.Missing")
    assert engine.hover(uri, lsp.Position(0, 15)) is None


@pytest.mark.parametrize(
    "target", ["app.models.Missing", "nonexistent_package_123.Model", "not valid", "a..b"]
)
def test_unresolved_target(python_project, target):
    root, engine = python_project
    uri = (root / "test.yaml").as_uri()
    engine.update(uri, f"_target_: '{target}'")
    diagnostics = engine.diagnostics(uri)
    assert len(diagnostics) == 1
    assert diagnostics[0].code == "unresolved-target"
    assert diagnostics[0].range.start.character == 10
    assert target in diagnostics[0].message


def test_non_scalar_target_is_reported(python_project):
    root, engine = python_project
    uri = (root / "test.yaml").as_uri()
    engine.update(uri, "_target_: [app.models.Model]")
    [diagnostic] = engine.diagnostics(uri)
    assert diagnostic.message == "_target_ must be a dotted Python path"


@pytest.mark.parametrize(
    "text",
    [
        "# _target_: missing.Class",
        "_target_: ${model.class}",
        "_target_: ???",
        'value: "_target_: missing.Class"',
        "description: |\n  _target_: missing.Class",
    ],
)
def test_ignored_targets(python_project, text):
    root, engine = python_project
    uri = (root / "test.yaml").as_uri()
    engine.update(uri, text)
    assert not engine.diagnostics(uri)
    assert not engine.python_targets(uri)


def test_aliased_targets_are_reported_once(python_project):
    root, engine = python_project
    uri = (root / "test.yaml").as_uri()
    engine.update(uri, "a: &m {_target_: app.models.Missing}\nb: *m\nc: *m")
    assert len(engine.diagnostics(uri)) == 1
    assert engine.python_targets(uri) == ["app.models.Missing"]


def test_block_scalar_is_not_a_target(python_project):
    root, engine = python_project
    uri = (root / "test.yaml").as_uri()
    engine.update(uri, "description: |\n  _target_: app.models.M")
    assert not engine.completions(uri, lsp.Position(1, 23))
    assert not engine.diagnostics(uri)


def test_ty_sees_python_changes_without_restarting(project):
    root, write = project
    models = write("models.py", "class Model: pass\n")
    uri = (root / "config.yaml").as_uri()
    with HydraDex([root]) as engine:
        engine.update(uri, "_target_: models.Model")
        backend = engine.python(uri)
        assert not engine.diagnostics(uri)
        write("models.py", "class Renamed: pass\n")
        # Cached until notified, like an editor without file watching.
        assert not engine.diagnostics(uri)
        engine.files_changed([lsp.FileEvent(models.as_uri(), lsp.FileChangeType.Changed)])
        assert engine.diagnostics(uri)[0].code == "unresolved-target"
        engine.update(uri, "_target_: models.Renamed")
        assert not engine.diagnostics(uri)
        assert engine.python(uri) is backend


def test_python_path_and_extra_paths(tmp_path):
    extra = tmp_path / "external"
    extra.mkdir()
    (extra / "custom.py").write_text("class Thing: pass\n")
    settings = Settings(python_path=sys.executable, extra_paths=(str(extra),))
    with HydraDex([tmp_path], settings) as engine:
        assert engine.python((tmp_path / "x.yaml").as_uri()).definitions("custom.Thing")


def test_shutdown_releases_process_and_can_restart(python_project):
    root, engine = python_project
    backend = engine.python((root / "test.yaml").as_uri())
    engine.shutdown()
    assert backend.client.process.returncode == 0
    assert not backend.alive
    assert engine.python((root / "test.yaml").as_uri()).definitions("app.models.Model")


def test_backend_crash_restarts(python_project):
    root, engine = python_project
    uri = (root / "test.yaml").as_uri()
    backend = engine.python(uri)
    backend.client.process.kill()
    backend.client.process.wait()
    replacement = engine.python(uri)
    assert replacement is not backend
    assert replacement.definitions("app.models.Model")


@pytest.mark.parametrize(
    ("target", "valid"),
    [
        ("app.Model", True),
        ("app.类", True),
        ("app.class", False),
        ("app.Model()", False),
        ("app..Model", False),
        ("", False),
    ],
)
def test_target_validation(target, valid):
    assert valid_target(target) is valid
    assert valid_target("app.", partial=True)


# Backend protocol behaviour, using the scripted fake backend.


def test_queries_are_cached_until_python_files_change(tmp_path):
    log = tmp_path / "log"
    backend = PythonAnalysis(tmp_path, fake_settings("--log", str(log)))
    try:

        def definition_requests():
            return sum(m.get("method") == "textDocument/definition" for m in logged(log))

        assert backend.resolved("pkg.Found")
        count = definition_requests()
        assert backend.resolved("pkg.Found")
        assert not backend.resolved("pkg.Missing")
        assert definition_requests() == count + 1
        event = lsp.FileEvent((tmp_path / "pkg.py").as_uri(), lsp.FileChangeType.Changed)
        backend.files_changed([event])
        assert backend.resolved("pkg.Found")
        assert definition_requests() > count + 1
        methods = [m.get("method") for m in logged(log)]
        assert methods.count("textDocument/didOpen") == 1
        assert "workspace/didChangeWatchedFiles" in methods
    finally:
        backend.close()
    assert "exit" in [m.get("method") for m in logged(log)]


def test_backend_answers_configuration_requests(tmp_path):
    log = tmp_path / "log"
    backend = PythonAnalysis(tmp_path, fake_settings("--log", str(log), extra_paths=("lib",)))
    backend.close()
    [response] = [m for m in logged(log) if m.get("id") == "config-1"]
    environment = response["result"][0]["configuration"]["environment"]
    assert str(tmp_path / "lib") in environment["extra-paths"]


def test_missing_backend_is_explicit(tmp_path):
    with pytest.raises(BackendError, match="Cannot start"):
        PythonAnalysis(tmp_path, Settings(backend_command=(str(tmp_path / "missing"),)))


def test_backend_initialize_timeout_cleans_up(tmp_path):
    settings = fake_settings("--hang-initialize", backend_timeout=0.2)
    with pytest.raises(BackendError, match="timed out"):
        PythonAnalysis(tmp_path, settings)


def test_failed_start_is_not_retried_immediately(tmp_path, monkeypatch):
    marker = tmp_path / "attempts"
    script = f"open({str(marker)!r}, 'a').write('x'); raise SystemExit(1)"
    settings = Settings(backend_command=(sys.executable, "-c", script))
    uri = (tmp_path / "x.yaml").as_uri()
    with HydraDex([tmp_path], settings) as engine:
        for _ in range(3):
            with pytest.raises(BackendError):
                engine.python(uri)
        assert marker.read_text() == "x"
        # A Python change may have fixed the environment, so it allows a retry.
        engine.files_changed([lsp.FileEvent((tmp_path / "a.py").as_uri(), 2)])
        with pytest.raises(BackendError):
            engine.python(uri)
        assert marker.read_text() == "xx"
        monkeypatch.setattr("hydradex.service.RETRY_DELAY", 0)
        with pytest.raises(BackendError):
            engine.python(uri)
        assert marker.read_text() == "xxx"


def test_backend_crash_during_request_surfaces_and_recovers(tmp_path):
    settings = fake_settings("--crash-on", "textDocument/hover")
    uri = (tmp_path / "x.yaml").as_uri()
    with HydraDex([tmp_path], settings) as engine:
        engine.update(uri, "_target_: pkg.Found")
        assert not engine.diagnostics(uri)
        with pytest.raises(BackendError):
            engine.hover(uri, lsp.Position(0, 12))
        assert not engine.diagnostics(uri)  # Cached result, no backend needed.
        assert engine.definitions(uri, lsp.Position(0, 12))  # Restarted backend.


def test_project_roots_for_files_outside_the_workspace(project):
    root, write = project
    write("elsewhere/pkg/pyproject.toml", "")
    loose = write("elsewhere/pkg/conf/x.yaml", "")
    bare = write("bare/x.yaml", "")
    with HydraDex([root / "workspace"]) as engine:
        assert engine.project_root(loose.as_uri()) == root / "elsewhere" / "pkg"
        assert engine.project_root((root / "workspace/a.yaml").as_uri()) == root / "workspace"
        assert engine.project_root(bare.as_uri()) in (root / "bare", *bare.parents)
