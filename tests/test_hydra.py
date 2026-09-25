"""Unit tests for layout and package rules; test_overrides checks them against Hydra."""

from pathlib import Path

import pytest

from hydradex import hydra

ROOT = Path("/work").resolve()


@pytest.mark.parametrize(
    ("path", "configured", "expected"),
    [
        ("conf/model/a.yaml", [], "conf"),
        ("app/configs/db/x.yaml", [], "app/configs"),
        ("conf/nested/conf/x.yaml", [], "conf/nested/conf"),
        ("settings/model/a.yaml", ["settings"], "settings"),
        ("settings/inner/a.yaml", ["settings", "settings/inner"], "settings/inner"),
        ("conf/a.yaml", ["settings"], "conf"),
        ("model/a.yaml", [], None),
    ],
)
def test_config_root(path, configured, expected):
    found = hydra.config_root(ROOT / path, ROOT, [ROOT / c for c in configured])
    assert found == (ROOT / expected if expected else None)


def test_config_root_ignores_conventional_names_outside_workspace():
    workspace = ROOT / "conf" / "project"
    assert hydra.config_root(workspace / "a.yaml", workspace, []) is None


def test_group():
    assert hydra.group(ROOT / "conf/db/mysql/x.yaml", ROOT / "conf") == ("db", "mysql")
    assert hydra.group(ROOT / "conf/x.yaml", ROOT / "conf") == ()
    assert hydra.group(ROOT / "other/x.yaml", ROOT / "conf") == ()


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("_global_", ()),
        ("", ()),
        ("_global_.x.y", ("x", "y")),
        ("x.y", ("x", "y")),
        # Hydra 1.3 treats these literally in headers.
        ("_group_", ("_group_",)),
        ("_here_", ("_here_",)),
    ],
)
def test_header_package_is_absolute(name, expected):
    assert hydra.header_package(name) == expected


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("net", ("outer", "net")),
        ("a.b", ("outer", "a", "b")),
        ("_here_", ("outer",)),
        ("_group_", ("outer", "db", "x")),
        ("_group_.extra", ("outer", "db", "x", "extra")),
        ("_global_", ()),
        ("_global_.top", ("top",)),
    ],
)
def test_override_package_is_relative_to_parent(name, expected):
    assert hydra.override_package(name, key=("db", "x"), parent=("outer",)) == expected


def test_child_package_precedence():
    kwargs = {"key": ("db",), "parent": ("server",)}
    assert hydra.child_package(None, None, **kwargs) == ("server", "db")
    assert hydra.child_package(None, "abs", **kwargs) == ("abs",)
    assert hydra.child_package("rel", "abs", **kwargs) == ("server", "rel")
