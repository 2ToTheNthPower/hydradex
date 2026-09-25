import pytest
from conftest import cursor
from lsprotocol import types as lsp

from hydradex.document import Document, defaults, header, uri_path


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


def test_positions_past_line_end_and_crlf():
    document = Document("untitled:test", "a: 1\r\nb: 2\r\n")
    assert not document.errors
    assert document.column(lsp.Position(0, 99)) == len("a: 1\r")
    assert document.column(lsp.Position(99, 0)) == 0
    assert document.position(99, 0).line == len(document.lines) - 1


@pytest.mark.parametrize("text", ["a: [", "a: *missing", "\tkey: value", "a: b: c"])
def test_malformed_yaml(text):
    document = Document("untitled:test", text)
    assert [error.code for error in document.errors] == ["yaml-syntax"]


def test_aliases_recursive_and_custom_tags():
    document = Document("untitled:test", "a: &a {self: *a, name: !custom value}\nb: *a")
    assert not document.errors
    assert len(document.entries) < 20
    assert ("b", "name") in {entry.path for entry in document.entries}


def test_alias_explosion_is_bounded():
    lines = ["a0: &a0 [x, x, x, x, x, x, x, x, x, x]"]
    lines += [f"a{i}: &a{i} [{', '.join([f'*a{i - 1}'] * 10)}]" for i in range(1, 8)]
    document = Document("untitled:test", "\n".join(lines))
    assert document.errors[0].code == "yaml-syntax"
    assert "limit" in document.errors[0].message


@pytest.mark.parametrize(
    ("text", "parent", "target", "prefix", "has_colon"),
    [
        ("_target_: app.Model\nwi|", (), "app.Model", "wi", False),
        ("model:\n  _target_: app.Model\n  |", ("model",), "app.Model", "", False),
        ("models:\n  - _target_: app.Model\n    wi|", ("models", "0"), "app.Model", "wi", False),
        ("_target_: app.Model\nchild:\n  wi|", ("child",), None, "wi", False),
        ("a: 1\nbi|as: true", (), None, "bi", True),
        ("a: 1\nbi| # comment", (), None, "bi", False),
    ],
)
def test_key_context(text, parent, target, prefix, has_colon):
    text, position = cursor(text)
    context = Document("untitled:test", text).key_context(position)
    assert context is not None
    assert (context.parent, context.target, context.prefix, context.has_colon) == (
        parent,
        target,
        prefix,
        has_colon,
    )


@pytest.mark.parametrize(
    "text", ["_target_: app.Model\n# wi|", "_target_: app.Model\nwidth: val|ue", "a: [x, y|]"]
)
def test_no_key_context_outside_keys(text):
    text, position = cursor(text)
    assert Document("untitled:test", text).key_context(position) is None


def test_key_context_span_covers_whole_word():
    text, position = cursor("a: 1\nbi|as")
    context = Document("untitled:test", text).key_context(position)
    assert (context.span.start.character, context.span.end.character) == (0, 4)
    assert context.siblings == {"a", "__hydradex_cursor__"}


def test_defaults_syntax():
    document = Document(
        "untitled:test",
        """defaults:
  - _self_
  - base
  - /group/item@here
  - override /model@alias: resnet
  - optional data: null
  - optional db: mysql
  - group: [one, two]
  - null
ordinary:
  - model: ignored
""",
    )
    found = defaults(document)
    assert [d.name for d in found] == [
        "base",
        "/group/item",
        "/model/resnet",
        "db/mysql",
        "group/one",
        "group/two",
    ]
    assert [d.package for d in found] == [None, "here", "alias", None, None, None]
    assert [d.optional for d in found] == [False, False, False, True, False, False]
    assert defaults(document, include_self=True)[0].name == "_self_"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("# @package foo\nx: 1", {"package": "foo"}),
        ("\n# comment\n  # @package  foo.bar \n\nx: 1", {"package": "foo.bar"}),
        ("x: 1\n# @package foo", {}),
        ("x: |\n  # @package _global_\n", {}),
        ("# @package\nx: 1", {"package": ""}),
        ("#@package _global_", {"package": "_global_"}),
    ],
)
def test_header_only_reads_leading_comments(text, expected):
    assert header(text) == expected
    assert Document("untitled:test", text).header == expected


def test_interpolation_excludes_comments_and_resolvers():
    document = Document("untitled:test", 'x: "😀 ${foo} ${oc.env:HOME}" # ${comment}')
    assert document.interpolation_at(lsp.Position(0, 11))[0] == "foo"
    assert document.interpolation_at(lsp.Position(0, 23)) is None
    assert document.interpolation_at(lsp.Position(0, 37)) is None


def test_nested_interpolation_resolves_the_inner_node():
    document = Document("untitled:test", "x: ${a.${b}}")
    assert document.interpolation_at(lsp.Position(0, 9))[0] == "b"
    assert document.interpolation_at(lsp.Position(0, 5)) is None


def test_uri_roundtrip(tmp_path):
    path = tmp_path / "space %23 # 😀.yaml"
    assert uri_path(path.as_uri()) == path
    assert uri_path("untitled:buffer") is None
    assert uri_path("file://remote-host/share/x.yaml") is None
