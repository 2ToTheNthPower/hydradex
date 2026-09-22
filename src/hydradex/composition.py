"""A static defaults-list view for editing overrides, with source provenance.

This follows literal file references and mapping merges without evaluating
resolvers or importing Python targets. Hydra itself still composes runnable apps.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from yaml.nodes import MappingNode

from hydradex.document import Document, Entry, defaults, uri_path

if TYPE_CHECKING:
    from hydradex.workspace import Workspace


@dataclass
class ConfigEntry:
    document: Document
    entry: Entry


def compose(workspace: Workspace, document: Document) -> dict[tuple[str, ...], ConfigEntry]:
    source = uri_path(document.uri)
    root = workspace.root_for(source) if source else None
    if source is None or root is None:
        return {}
    configured = workspace.settings.paths(workspace.settings.config_roots, root)
    ancestors = [source.parent, *source.parent.parents]
    config_root = next((p for p in configured if source.is_relative_to(p)), None)
    if config_root is None:
        config_root = next(
            (
                p
                for p in ancestors
                if p.name in ("conf", "config", "configs") and p.is_relative_to(root)
            ),
            source.parent,
        )
    result: dict[tuple[str, ...], ConfigEntry] = {}
    budget = 256

    def merge(current: Document, package: tuple[str, ...]) -> None:
        for entry in current.entries:
            if entry.path[0] == "defaults" or "__hydradex_cursor__" in entry.path:
                continue
            path = (*package, *entry.path)
            # Mapping nodes merge. Scalars and lists replace the entire subtree.
            if not isinstance(entry.value, MappingNode):
                for old in list(result):
                    if old[: len(path)] == path:
                        del result[old]
            result[path] = ConfigEntry(current, entry)

    def visit(current: Document, package: tuple[str, ...], active: frozenset[str]) -> None:
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
            if location is None:
                continue
            path = uri_path(location.uri)
            if path is None:
                continue
            target = workspace.documents.get(location.uri)
            if target is None:
                try:
                    target = Document(location.uri, path.read_text(encoding="utf-8"))
                except (OSError, UnicodeError):
                    continue
            group = (
                path.parent.relative_to(config_root).parts
                if path.is_relative_to(config_root)
                else ()
            )
            directive = re.search(r"^\s*#\s*@package\s+(\S+)", target.text, re.MULTILINE)
            package_name = reference.package or (directive[1] if directive else None)
            if package_name == "_global_":
                child_package = ()
            elif package_name == "_here_":
                child_package = package
            elif package_name == "_group_" or package_name is None:
                child_package = group
            elif package_name.startswith("_global_."):
                child_package = tuple(package_name.removeprefix("_global_.").split("."))
            else:
                # Defaults package overrides are relative; file directives are absolute.
                child_package = (
                    (*package, *package_name.split("."))
                    if reference.package
                    else tuple(package_name.split("."))
                )
            visit(target, child_package, active)
        if not any(reference.name == "_self_" for reference in references):
            merge(current, package)

    visit(document, (), frozenset())
    return result
