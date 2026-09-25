from pathlib import Path

import pytest

from hydradex import Settings
from hydradex.settings import settings_section


@pytest.mark.parametrize(
    "options",
    [
        {"matchFilter": "bad"},
        {"excludePatterns": "bad"},
        {"excludePatterns": [1]},
        {"pythonPath": 123},
        {"isolateWorkspaceFolders": "yes"},
        {"backendTimeout": 0},
        {"backendTimeout": True},
        {"backendTimeout": float("inf")},
        {"backendCommand": "ty server"},
    ],
)
def test_invalid_values(options):
    with pytest.raises(ValueError):
        Settings.from_lsp({"hydradex": options})


def test_flat_or_nested_initialization_options():
    flat, _ = Settings.from_lsp({"configRoots": ["conf"]})
    nested, _ = Settings.from_lsp({"hydradex": {"configRoots": ["conf"]}})
    assert flat == nested == Settings(config_roots=("conf",))
    assert Settings.from_lsp(None) == (Settings(), [])
    with pytest.raises(ValueError):
        Settings.from_lsp(["not", "an", "object"])


def test_lists_are_normalized_so_equal_settings_compare_equal():
    settings, _ = Settings.from_lsp({"extraPaths": ["a", "b"]})
    assert settings.extra_paths == ("a", "b")
    assert settings == Settings(extra_paths=("a", "b"))
    assert hash(settings) == hash(Settings(extra_paths=["a", "b"]))


def test_merge_keeps_absent_keys_and_null_restores_default():
    base = Settings(python_path="/venv/bin/python", config_roots=("conf",))
    merged, warnings = base.merge({"matchFilter": "all", "configRoots": None})
    assert merged.python_path == "/venv/bin/python"
    assert merged.match_filter == "all"
    assert merged.config_roots == ()
    assert warnings == []


def test_unknown_keys_are_reported_not_fatal():
    settings, warnings = Settings.from_lsp({"configRoot": ["conf"], "matchFilter": "all"})
    assert settings.match_filter == "all"
    assert warnings == ["Unknown HydraDex setting 'configRoot'"]


@pytest.mark.parametrize(
    ("payload", "section"),
    [
        ({"hydradex": {"matchFilter": "all"}}, {"matchFilter": "all"}),
        ({"yaml": {"schemas": {}}}, None),
        (None, None),
        ({"hydradex": None}, None),
        ({"matchFilter": "all"}, None),
    ],
)
def test_change_notifications_require_a_hydradex_section(payload, section):
    assert settings_section(payload) == section


def test_paths_are_relative_to_root(tmp_path):
    settings = Settings(extra_paths=("lib", str(tmp_path / "abs")))
    assert settings.paths(settings.extra_paths, tmp_path) == [tmp_path / "lib", tmp_path / "abs"]
    home = Settings().paths(("~/x",), tmp_path)[0]
    assert home == (Path.home() / "x").resolve()
