"""A static defaults-list view for editing overrides, with source provenance.

This follows literal file references and mapping merges without evaluating
resolvers or importing Python targets. Hydra itself still composes runnable apps.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from yaml.nodes import MappingNode

from hydradex import hydra
from hydradex.document import CURSOR, Document, Entry, defaults, uri_path

if TYPE_CHECKING:
    from hydradex.workspace import Workspace

_VISIT_LIMIT = 256


@dataclass
class ConfigEntry:
    document: Document
    entry: Entry


def compose(workspace: Workspace, document: Document) -> dict[tuple[str, ...], ConfigEntry]:
    """Compose `document` as a primary config: its keys are placed at the root."""
    source = uri_path(document.uri)
    root = workspace.root_for(source) if source else None
    if source is None or root is None:
        return {}
    result: dict[tuple[str, ...], ConfigEntry] = {}
    budget = _VISIT_LIMIT

    def merge(current: Document, package: hydra.Package) -> None:
        for entry in current.entries:
            if entry.path[0] == "defaults" or CURSOR in entry.path:
                continue
            path = (*package, *entry.path)
            # Mappings merge; scalars and lists replace the entire subtree.
            if not isinstance(entry.value, MappingNode):
                for old in [key for key in result if key[: len(path)] == path]:
                    del result[old]
            result[path] = ConfigEntry(current, entry)

    def visit(current: Document, package: hydra.Package, active: frozenset[str]) -> None:
        nonlocal budget
        if current.uri in active or budget <= 0:
            return
        budget -= 1
        active = active | {current.uri}
        references = defaults(current, include_self=True)
        for reference in references:
            if reference.name == "_self_":
                merge(current, package)
                continue
            location = workspace.default_location(current, reference)
            path = uri_path(location.uri) if location else None
            target = workspace.read(path) if path else None
            if path is None or target is None:
                continue
            child = hydra.child_package(
                reference.package,
                target.header.get("package"),
                key=reference.group,
                parent=package,
            )
            visit(target, child, active)
        if not any(reference.name == "_self_" for reference in references):
            merge(current, package)

    # A primary config's own header places everything it composes.
    primary = document.header.get("package")
    visit(document, hydra.header_package(primary) if primary else (), frozenset())
    return result
