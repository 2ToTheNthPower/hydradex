"""Position-preserving YAML syntax without constructing application objects."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import url2pathname

import yaml
from lsprotocol import types as lsp
from yaml.nodes import MappingNode, Node, ScalarNode, SequenceNode

NULL_TAG = "tag:yaml.org,2002:null"
CURSOR = "__hydradex_cursor__"
_NODE_LIMIT = 20000


def uri_path(uri: str) -> Path | None:
    parsed = urlparse(uri)
    if parsed.scheme != "file" or parsed.netloc not in ("", "localhost"):
        return None
    return Path(url2pathname(parsed.path)).resolve()


def utf16(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def header(text: str) -> dict[str, str]:
    """Hydra `# @key value` directives, read only from the leading comment block."""
    result = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if not stripped.startswith("#"):
            break
        comment = stripped[1:].strip()
        if comment.startswith("@"):
            key, _, value = comment[1:].partition(" ")
            if key:
                result[key] = value.strip()
    return result


@dataclass
class Entry:
    path: tuple[str, ...]
    key: ScalarNode
    value: Node
    mapping: MappingNode


@dataclass
class KeyContext:
    """A mapping key being typed, located by re-parsing with the key repaired."""

    document: Document
    parent: tuple[str, ...]
    siblings: set[str]
    prefix: str
    span: lsp.Range
    has_colon: bool
    target: str | None


@dataclass
class Document:
    uri: str
    text: str
    version: int | None = None
    entries: list[Entry] = field(default_factory=list, init=False)
    errors: list[lsp.Diagnostic] = field(default_factory=list, init=False)

    def __post_init__(self) -> None:
        self.lines = self.text.split("\n")
        self.header = header(self.text)
        try:
            roots = [node for node in yaml.compose_all(self.text, Loader=yaml.SafeLoader) if node]
            budget = [_NODE_LIMIT]
            for node in roots:
                self._walk(node, (), frozenset(), budget)
        except (yaml.YAMLError, RecursionError, ValueError) as exc:
            mark = getattr(exc, "problem_mark", None)
            line, col = (mark.line, mark.column) if mark else (0, 0)
            self.errors.append(
                lsp.Diagnostic(
                    range=self.range(line, col, line, col + 1),
                    message=getattr(exc, "problem", None) or str(exc),
                    severity=lsp.DiagnosticSeverity.Error,
                    source="hydradex",
                    code="yaml-syntax",
                )
            )

    def _walk(
        self, node: Node, path: tuple[str, ...], ancestors: frozenset[int], budget: list[int]
    ) -> None:
        if id(node) in ancestors:
            return
        budget[0] -= 1
        if budget[0] < 0:
            raise ValueError("YAML structure exceeds the indexing limit")
        ancestors = ancestors | {id(node)}
        if isinstance(node, MappingNode):
            for key, value in node.value:
                if isinstance(key, ScalarNode):
                    child = (*path, key.value)
                    self.entries.append(Entry(child, key, value, node))
                    self._walk(value, child, ancestors, budget)
        elif isinstance(node, SequenceNode):
            for i, value in enumerate(node.value):
                self._walk(value, (*path, str(i)), ancestors, budget)

    # Positions: PyYAML marks count code points; LSP positions count UTF-16 units.

    def position(self, line: int, column: int) -> lsp.Position:
        line = max(0, min(line, len(self.lines) - 1))
        return lsp.Position(line, utf16(self.lines[line][:column]))

    def range(self, line: int, col: int, end_line: int, end_col: int) -> lsp.Range:
        return lsp.Range(self.position(line, col), self.position(end_line, end_col))

    def node_range(self, node: Node) -> lsp.Range:
        return self.range(
            node.start_mark.line, node.start_mark.column, node.end_mark.line, node.end_mark.column
        )

    def column(self, position: lsp.Position) -> int:
        if not 0 <= position.line < len(self.lines):
            return 0
        units = 0
        for i, char in enumerate(self.lines[position.line]):
            units += utf16(char)
            if units > position.character:
                return i
        return len(self.lines[position.line])

    def contains(self, node: Node, position: lsp.Position) -> bool:
        point = (position.line, self.column(position))
        return (
            (node.start_mark.line, node.start_mark.column)
            <= point
            < (node.end_mark.line, node.end_mark.column)
        )

    def targets(self) -> list[Entry]:
        return [entry for entry in self.entries if entry.key.value == "_target_"]

    def target_at(self, position: lsp.Position) -> Entry | None:
        return next(
            (
                entry
                for entry in self.targets()
                if isinstance(entry.value, ScalarNode) and self.contains(entry.value, position)
            ),
            None,
        )

    def interpolation_at(self, position: lsp.Position) -> tuple[str, Entry] | None:
        """A plain `${node.path}` under the cursor, inside a scalar value."""
        if not 0 <= position.line < len(self.lines):
            return None
        col = self.column(position)
        for match in re.finditer(r"\$\{([^${}:]+)\}", self.lines[position.line]):
            if match.start() <= col < match.end():
                for entry in self.entries:
                    if isinstance(entry.value, ScalarNode) and self.contains(entry.value, position):
                        return match[1].strip(), entry
        return None

    def key_context(self, position: lsp.Position) -> KeyContext | None:
        """Repair only the edited key, letting the YAML parser determine its mapping."""
        if not 0 <= position.line < len(self.lines):
            return None
        line = self.lines[position.line]
        col = self.column(position)
        match = re.fullmatch(r"(\s*(?:-\s+)?)(\w*)", line[:col])
        if not match:
            return None
        suffix = re.match(r"\w*", line[col:])
        end = col + (len(suffix[0]) if suffix else 0)
        tail = line[end:]
        has_colon = tail.lstrip().startswith(":")
        if tail.strip() and not has_colon and not tail.lstrip().startswith("#"):
            return None
        lines = self.lines.copy()
        lines[position.line] = match[1] + CURSOR + (tail if has_colon else ": null")
        repaired = Document(self.uri, "\n".join(lines))
        entry = next((e for e in repaired.entries if e.key.value == CURSOR), None)
        if entry is None:
            return None
        siblings = {key.value for key, _ in entry.mapping.value if isinstance(key, ScalarNode)}
        target = next(
            (
                value.value
                for key, value in entry.mapping.value
                if isinstance(key, ScalarNode)
                and key.value == "_target_"
                and isinstance(value, ScalarNode)
            ),
            None,
        )
        return KeyContext(
            repaired,
            entry.path[:-1],
            siblings,
            match[2],
            self.range(position.line, len(match[1]), position.line, end),
            has_colon,
            target,
        )


@dataclass(frozen=True)
class Default:
    """One defaults-list reference such as `group@package: option` or `_self_`."""

    name: str
    node: ScalarNode
    package: str | None = None
    optional: bool = False

    @property
    def group(self) -> tuple[str, ...]:
        """The config group as written, e.g. `("db",)` for `/db: mysql` or `db/mysql`."""
        return tuple(self.name.lstrip("/").split("/")[:-1])


def defaults(document: Document, *, include_self: bool = False) -> list[Default]:
    result: list[Default] = []
    for entry in document.entries:
        if entry.path != ("defaults",) or not isinstance(entry.value, SequenceNode):
            continue
        for item in entry.value.value:
            if isinstance(item, ScalarNode):
                if item.tag == NULL_TAG or (item.value == "_self_" and not include_self):
                    continue
                name, _, package = item.value.partition("@")
                result.append(Default(name, item, package or None))
            elif isinstance(item, MappingNode):
                for group, option in item.value:
                    if not isinstance(group, ScalarNode):
                        continue
                    keywords = re.match(r"^(?:(?:override|optional)\s+)*", group.value)
                    optional = "optional" in keywords[0].split() if keywords else False
                    name, _, package = group.value[keywords.end() if keywords else 0 :].partition(
                        "@"
                    )
                    options = option.value if isinstance(option, SequenceNode) else [option]
                    for value in options:
                        if isinstance(value, ScalarNode) and value.tag != NULL_TAG:
                            result.append(
                                Default(f"{name}/{value.value}", value, package or None, optional)
                            )
    return result
