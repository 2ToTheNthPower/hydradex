import pytest
from lsprotocol import types as lsp

from hydradex import HydraDex, Settings
from hydradex.python import BackendError, PythonAnalysis, valid_target


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
    assert engine.hover(uri, lsp.Position(0, 0)) is None


@pytest.mark.parametrize(
    "target", ["app.models.Missing", "nonexistent_package_123.Model", "not valid"]
)
def test_unresolved_target(python_project, target):
    root, engine = python_project
    uri = (root / "test.yaml").as_uri()
    engine.update(uri, f"_target_: '{target}'")
    diagnostics = engine.diagnostics(uri)
    assert len(diagnostics) == 1
    assert diagnostics[0].code == "unresolved-target"
    assert diagnostics[0].range.start.character == 10


@pytest.mark.parametrize(
    "text",
    [
        "# _target_: missing.Class",
        "_target_: ${model.class}",
        'value: "_target_: missing.Class"',
    ],
)
def test_ignored_targets(python_project, text):
    root, engine = python_project
    uri = (root / "test.yaml").as_uri()
    engine.update(uri, text)
    assert not engine.diagnostics(uri)


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


def test_missing_backend_is_explicit(tmp_path):
    with pytest.raises(BackendError, match="No such file|cannot find"):
        PythonAnalysis(tmp_path, Settings(backend_command=(str(tmp_path / "missing"),)))


def test_shutdown_releases_process_and_can_restart(python_project):
    root, engine = python_project
    backend = engine.python((root / "test.yaml").as_uri())
    process = backend.client._server
    engine.shutdown()
    assert process.returncode == 0
    assert not backend.thread.is_alive()
    assert engine.python((root / "test.yaml").as_uri()).definitions("app.models.Model")


def test_python_path_and_extra_paths(tmp_path):
    extra = tmp_path / "external"
    extra.mkdir()
    (extra / "custom.py").write_text("class Thing: pass\n")
    import sys

    with HydraDex(
        [tmp_path],
        Settings(python_path=sys.executable, extra_paths=(str(extra),)),
    ) as engine:
        assert engine.python((tmp_path / "x.yaml").as_uri()).definitions("custom.Thing")


def test_backend_timeout_cleans_up(tmp_path):
    import sys

    command = (sys.executable, "-c", "import time; time.sleep(60)")
    with pytest.raises(BackendError, match="timed out"):
        PythonAnalysis(tmp_path, Settings(backend_command=command, backend_timeout=0.1))


def test_backend_crash_restarts(python_project):
    root, engine = python_project
    uri = (root / "test.yaml").as_uri()
    backend = engine.python(uri)
    process = backend.client._server
    backend.loop.call_soon_threadsafe(process.kill)
    backend._run(process.wait())
    replacement = engine.python(uri)
    assert replacement is not backend
    assert replacement.definitions("app.models.Model")
    assert not backend.thread.is_alive()


@pytest.mark.parametrize("prefix", ["", "app.", "app.models."])
def test_module_completion_levels(python_project, prefix):
    root, engine = python_project
    uri = (root / "test.yaml").as_uri()
    engine.update(uri, f"_target_: {prefix}")
    items = engine.completions(uri, lsp.Position(0, 10 + len(prefix)))
    assert items
    if prefix == "app.models.":
        assert {"Model", "Data", "factory"} <= {item.label for item in items}


def test_block_scalar_is_not_a_target(python_project):
    root, engine = python_project
    uri = (root / "test.yaml").as_uri()
    engine.update(uri, "description: |\n  _target_: app.models.M")
    assert not engine.completions(uri, lsp.Position(1, 23))
    assert not engine.diagnostics(uri)
