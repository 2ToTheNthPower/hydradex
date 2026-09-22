import pytest
from lsprotocol import types as lsp

from hydradex import HydraDex, Settings


def test_interpolation_ranking_and_multiple_keys_per_file(project):
    root, write = project
    first = write("conf/dataset/a.yaml", "name: one\nnested:\n  name: two\n")
    second = write("conf/other.yaml", "name: three")
    source = write("conf/main.yaml", "value: ${dataset.name}")
    for mode, count in [("all", 3), ("top matches only", 1), ("perfect matches only", 1)]:
        with HydraDex([root], Settings(match_filter=mode)) as engine:
            locations = engine.definitions(source.as_uri(), lsp.Position(0, 14))
            assert len(locations) == count
            assert locations[0].uri == first.as_uri()
            if mode == "all":
                assert second.as_uri() in [loc.uri for loc in locations]


def test_isolation_precedes_ranking(project):
    root, write = project
    a = root / "a"
    b = root / "b"
    source = write("a/main.yaml", "value: ${dataset.name}")
    local = write("a/local.yaml", "name: one")
    remote = write("b/dataset/remote.yaml", "name: two")
    with HydraDex([a, b]) as engine:
        assert engine.definitions(source.as_uri(), lsp.Position(0, 14))[0].uri == local.as_uri()
    with HydraDex([a, b], Settings(isolate_workspace_folders=False)) as engine:
        assert engine.definitions(source.as_uri(), lsp.Position(0, 14))[0].uri == remote.as_uri()


def test_perfect_filter_rejects_partial_match(project):
    root, write = project
    source = write("main.yaml", "name: one\nref: ${missing.name}")
    with HydraDex([root], Settings(match_filter="perfect matches only")) as engine:
        assert not engine.definitions(source.as_uri(), lsp.Position(1, 10))


def test_overlays_change_delete_close_refresh(project):
    root, write = project
    source = write("main.yaml", "ref: ${name}")
    target = write("data.yaml", "name: old")
    with HydraDex([root]) as engine:
        position = lsp.Position(0, 9)
        assert len(engine.definitions(source.as_uri(), position)) == 1
        engine.update(target.as_uri(), "different: new", 2)
        assert not engine.definitions(source.as_uri(), position)
        engine.refresh()
        assert not engine.definitions(source.as_uri(), position)
        engine.workspace.changed(target.as_uri())
        assert not engine.definitions(source.as_uri(), position)
        engine.close(target.as_uri())
        assert engine.definitions(source.as_uri(), position)
        target.unlink()
        engine.workspace.changed(target.as_uri(), deleted=True)
        assert not engine.definitions(source.as_uri(), position)
        write("data.yaml", "name: recreated")
        engine.workspace.changed(target.as_uri())
        assert engine.definitions(source.as_uri(), position)


def test_exclusions_and_invalid_utf8(project):
    root, write = project
    write(".venv/lib/bad.yaml", "name: bad")
    write("node_modules/bad.yaml", "name: bad")
    write("generated/output.yaml", "name: bad")
    source = write("main.yaml", "name: good\nref: ${name}")
    (root / "invalid.yaml").write_bytes(b"\xff\xff")
    with HydraDex(
        [root], Settings(exclude_patterns=(".venv/", "node_modules/", "generated/"))
    ) as engine:
        assert len(engine.definitions(source.as_uri(), lsp.Position(1, 9))) == 1
        assert len(engine.workspace.documents) == 1


@pytest.mark.parametrize(("expression", "line"), [(".name", 1), ("..name", 0)])
def test_relative_interpolation(project, expression, line):
    root, write = project
    source = write("main.yaml", f"name: root\nobj: {{name: child, ref: '${{{expression}}}'}}")
    with HydraDex([root]) as engine:
        locations = engine.definitions(source.as_uri(), lsp.Position(1, 29))
        assert len(locations) == 1
        assert locations[0].range.start.line == line


def test_package_directive(project):
    root, write = project
    target = write("conf/model/a.yaml", "# @package training\nrate: 3")
    source = write("conf/main.yaml", "ref: ${training.rate}")
    with HydraDex([root], Settings(match_filter="perfect matches only")) as engine:
        assert engine.definitions(source.as_uri(), lsp.Position(0, 13))[0].uri == target.as_uri()


@pytest.mark.parametrize(
    "entry",
    [
        "model: small",
        "override model@network: small",
        "optional /model: small",
        "model/small",
        "/model/small.yml",
        "model: [small]",
    ],
)
def test_defaults_navigation_and_hover(project, entry):
    root, write = project
    target = write("conf/model/small.yml", "width: 3")
    source = write("conf/main.yaml", f"defaults:\n  - {entry}")
    position = lsp.Position(1, len("  - " + entry) - (2 if entry.endswith("]") else 1))
    with HydraDex([root]) as engine:
        assert engine.definitions(source.as_uri(), position)[0].uri == target.as_uri()
        assert target.as_uri() in engine.hover(source.as_uri(), position).contents.value


def test_defaults_missing_and_escape(project):
    root, write = project
    source = write("main.yaml", "defaults:\n  - missing\n  - ../outside")
    with HydraDex([root]) as engine:
        assert not engine.definitions(source.as_uri(), lsp.Position(1, 7))
        assert "[not found]" in engine.hover(source.as_uri(), lsp.Position(1, 7)).contents.value
        assert not engine.definitions(source.as_uri(), lsp.Position(2, 7))


def test_custom_config_roots(project):
    root, write = project
    target = write("settings/model/small.yaml", "width: 3")
    source = write("experiments/main.yaml", "defaults:\n  - /model: small")
    with HydraDex([root], Settings(config_roots=("settings",))) as engine:
        assert engine.definitions(source.as_uri(), lsp.Position(1, 13))[0].uri == target.as_uri()


@pytest.mark.parametrize(
    "options",
    [
        {"matchFilter": "bad"},
        {"excludePatterns": "bad"},
        {"pythonPath": 123},
        {"isolateWorkspaceFolders": "yes"},
        {"backendTimeout": 0},
    ],
)
def test_invalid_settings(options):
    with pytest.raises(ValueError):
        Settings.from_lsp({"hydradex": options})
