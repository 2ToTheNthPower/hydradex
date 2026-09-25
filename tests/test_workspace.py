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


def test_nested_workspace_folders_own_their_files(project):
    root, write = project
    inner = write("inner/data.yaml", "name: inner")
    write("outer.yaml", "name: outer")
    source = write("inner/main.yaml", "ref: ${name}")
    with HydraDex([root, root / "inner"]) as engine:
        assert engine.workspace.root_for(inner) == root / "inner"
        locations = engine.definitions(source.as_uri(), lsp.Position(0, 8))
        assert [loc.uri for loc in locations] == [inner.as_uri()]


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


def test_deleted_directory_removes_contained_files(project):
    root, write = project
    source = write("main.yaml", "ref: ${name}")
    target = write("group/data.yaml", "name: x")
    opened = write("group/open.yaml", "name: y")
    with HydraDex([root]) as engine:
        engine.update(opened.as_uri(), "name: y")
        for path in (target, opened):
            path.unlink()
        (root / "group").rmdir()
        deleted = lsp.FileEvent((root / "group").as_uri(), lsp.FileChangeType.Deleted)
        engine.files_changed([deleted])
        locations = engine.definitions(source.as_uri(), lsp.Position(0, 8))
        assert [loc.uri for loc in locations] == [opened.as_uri()]


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


def test_unsaved_buffers_outside_the_workspace_still_work(project):
    root, write = project
    with HydraDex([root / "elsewhere"]) as engine:
        uri = (root / "loose.yaml").as_uri()
        engine.update(uri, "name: x\nref: ${.name}")
        assert engine.definitions(uri, lsp.Position(1, 9))[0].range.start.line == 0
        engine.update("untitled:Untitled-1", "a: [")
        assert engine.diagnostics("untitled:Untitled-1")[0].code == "yaml-syntax"


@pytest.mark.parametrize(("expression", "line"), [(".name", 1), ("..name", 0)])
def test_relative_interpolation(project, expression, line):
    root, write = project
    source = write("main.yaml", f"name: root\nobj: {{name: child, ref: '${{{expression}}}'}}")
    with HydraDex([root]) as engine:
        locations = engine.definitions(source.as_uri(), lsp.Position(1, 29))
        assert len(locations) == 1
        assert locations[0].range.start.line == line


def test_relative_interpolation_too_many_levels(project):
    root, write = project
    source = write("main.yaml", "obj: {ref: '${...name}'}")
    with HydraDex([root]) as engine:
        assert not engine.definitions(source.as_uri(), lsp.Position(0, 15))


def test_relative_interpolation_falls_back_to_same_package(project):
    root, write = project
    sibling = write("conf/model/base.yaml", "# @package model\nwidth: 4\n")
    source = write("conf/model/small.yaml", "# @package model\nhidden: ${.width}\n")
    with HydraDex([root]) as engine:
        locations = engine.definitions(source.as_uri(), lsp.Position(1, 12))
        assert [loc.uri for loc in locations] == [sibling.as_uri()]


@pytest.mark.parametrize(
    ("header", "expression"),
    [
        ("# @package training\n", "training.rate"),
        ("# @package _global_.training\n", "training.rate"),
        ("# @package _global_\n", "rate"),
        ("", "model.rate"),
        ("# comment first\n\n# @package training\n", "training.rate"),
    ],
)
def test_package_directive(project, header, expression):
    root, write = project
    target = write("conf/model/a.yaml", header + "rate: 3")
    source = write("conf/main.yaml", f"ref: ${{{expression}}}")
    with HydraDex([root], Settings(match_filter="perfect matches only")) as engine:
        locations = engine.definitions(source.as_uri(), lsp.Position(0, 8))
        assert [loc.uri for loc in locations] == [target.as_uri()]


def test_package_directive_after_content_is_ignored(project):
    root, write = project
    target = write("conf/model/a.yaml", "rate: 3\n# @package _global_\n")
    source = write("conf/main.yaml", "ref: ${model.rate}")
    with HydraDex([root], Settings(match_filter="perfect matches only")) as engine:
        locations = engine.definitions(source.as_uri(), lsp.Position(0, 8))
        assert [loc.uri for loc in locations] == [target.as_uri()]
        assert [d.path for d in engine.workspace.index[("rate",)]] == [("model", "rate")]


def test_primary_config_keys_are_perfect_matches(project):
    root, write = project
    target = write("app/conf/config.yaml", "training:\n  epochs: 5\n")
    source = write("app/conf/experiment.yaml", "steps: ${training.epochs}")
    with HydraDex([root], Settings(match_filter="perfect matches only")) as engine:
        locations = engine.definitions(source.as_uri(), lsp.Position(0, 12))
        assert [loc.uri for loc in locations] == [target.as_uri()]


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


def test_defaults_missing_optional_and_escape(project):
    root, write = project
    source = write("main.yaml", "defaults:\n  - missing\n  - ../outside\n  - optional db: none")
    write("../outside.yaml", "x: 1")
    with HydraDex([root]) as engine:
        assert not engine.definitions(source.as_uri(), lsp.Position(1, 7))
        hover = engine.hover(source.as_uri(), lsp.Position(1, 7)).contents.value
        assert hover == "missing [not found]"
        assert not engine.definitions(source.as_uri(), lsp.Position(2, 7))
        hover = engine.hover(source.as_uri(), lsp.Position(3, 18)).contents.value
        assert hover == "db/none [optional, not found]"


def test_absolute_defaults_from_nested_group(project):
    root, write = project
    target = write("conf/model/small.yaml", "width: 3")
    source = write("conf/experiment/big.yaml", "defaults:\n  - override /model: small")
    with HydraDex([root]) as engine:
        locations = engine.definitions(source.as_uri(), lsp.Position(1, 22))
        assert locations[0].uri == target.as_uri()


def test_custom_config_roots(project):
    root, write = project
    target = write("settings/model/small.yaml", "width: 3")
    source = write("experiments/main.yaml", "defaults:\n  - /model: small")
    with HydraDex([root], Settings(config_roots=("settings",))) as engine:
        assert engine.definitions(source.as_uri(), lsp.Position(1, 13))[0].uri == target.as_uri()


def test_reconfigure_preserves_open_buffers(project):
    root, write = project
    source = write("main.yaml", "ref: ${name}")
    target = write("data.yaml", "name: disk")
    other = root / "other"
    other.mkdir()
    with HydraDex([root]) as engine:
        engine.update(target.as_uri(), "renamed: buffer")
        engine.reconfigure(settings=Settings(match_filter="all"))
        assert engine.settings.match_filter == "all"
        assert not engine.definitions(source.as_uri(), lsp.Position(0, 8))
        engine.reconfigure(roots=[root, other])
        assert engine.roots == [other, root]
        assert engine.workspace.documents[target.as_uri()].text == "renamed: buffer"
        engine.close(target.as_uri())
        assert engine.definitions(source.as_uri(), lsp.Position(0, 8))
