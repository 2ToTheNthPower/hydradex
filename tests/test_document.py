import pytest
from lsprotocol import types as lsp

from hydradex.document import Document, defaults, uri_path


def test_nested_flow_lists_quoted_keys_and_multidocument():
    document = Document(
        "untitled:test",
        """model:
  "key: with colon": {name: test}
  layers:
    - width: 2
---
other: value
""",
    )
    paths = {entry.path for entry in document.entries}
    assert ("model", "key: with colon", "name") in paths
    assert ("model", "layers", "0", "width") in paths
    assert ("other",) in paths
    assert not document.errors


def test_utf16_positions():
    document = Document("untitled:test", "😀: {model: {_target_: app.Model}}")
    target = document.entries[-1]
    span = document.node_range(target.value)
    assert span.start.character == document.text.index("app.Model") + 1
    assert document.column(span.start) == document.text.index("app.Model")
    assert document.target_at(span.start) == target


@pytest.mark.parametrize("text", ["a: [", "a: *missing", "\tkey: value"])
def test_malformed_yaml(text):
    document = Document("untitled:test", text)
    assert document.errors[0].code == "yaml-syntax"


def test_aliases_recursive_and_custom_tags():
    document = Document("untitled:test", "a: &a {self: *a, name: !custom value}\nb: *a")
    assert not document.errors
    assert len(document.entries) < 20
    assert ("b", "name") in {entry.path for entry in document.entries}


@pytest.mark.parametrize(
    ("text", "line", "col", "target"),
    [
        ("_target_: app.Model\nwi", 1, 2, "app.Model"),
        ("model:\n  _target_: app.Model\n  ", 2, 2, "app.Model"),
        ("models:\n  - _target_: app.Model\n    wi", 2, 6, "app.Model"),
        ("_target_: app.Model\nchild:\n  wi", 2, 4, None),
        ("_target_: app.Model\n# wi", 1, 4, None),
        ("_target_: app.Model\nwidth: value", 1, 10, None),
    ],
)
def test_parameter_mapping_scope(text, line, col, target):
    context = Document("untitled:test", text).parameter_context(lsp.Position(line, col))
    assert (context[0] if context else None) == target


def test_defaults_syntax():
    document = Document(
        "untitled:test",
        """defaults:
  - _self_
  - base
  - /group/item@here
  - override /model@alias: resnet
  - optional data: null
  - group: [one, two]
ordinary:
  - model: ignored
""",
    )
    assert [d.name for d in defaults(document)] == [
        "base",
        "/group/item",
        "/model/resnet",
        "group/one",
        "group/two",
    ]


def test_interpolation_excludes_comments_and_resolvers():
    document = Document("untitled:test", 'x: "😀 ${foo} ${oc.env:HOME}" # ${comment}')
    assert document.interpolation_at(lsp.Position(0, 11))[0] == "foo"
    assert document.interpolation_at(lsp.Position(0, 23)) is None
    assert document.interpolation_at(lsp.Position(0, 37)) is None


def test_uri_roundtrip(tmp_path):
    path = tmp_path / "space %23 # 😀.yaml"
    assert uri_path(path.as_uri()) == path
    assert uri_path("untitled:buffer") is None
