"""Defaults-aware editing, checked against Hydra's real composition semantics."""

import json
import subprocess
import sys

import pytest
from conftest import REPO, cursor
from hydra import compose as hydra_compose
from hydra import initialize_config_dir
from omegaconf import OmegaConf
from yaml.nodes import MappingNode

from hydradex import HydraDex
from hydradex.composition import compose


def completions(engine, path, text):
    """Complete at the `|` cursor without saving the incomplete YAML file."""
    text, position = cursor(text)
    engine.update(path.as_uri(), text)
    return {item.label: item for item in engine.completions(path.as_uri(), position)}


def hydra_leaves(config_dir, name="main"):
    with initialize_config_dir(version_base=None, config_dir=str(config_dir)):
        config = OmegaConf.to_container(hydra_compose(config_name=name), resolve=False)

    def walk(node, path):
        if isinstance(node, dict) and node:
            for key, value in node.items():
                yield from walk(value, (*path, str(key)))
        else:
            yield path, node

    return dict(walk(config, ()))


def hydradex_leaves(root, source):
    with HydraDex([root]) as engine:
        composed = compose(engine.workspace, engine.document(source.as_uri()))
    return {
        path: value.entry.value.value
        for path, value in composed.items()
        if not isinstance(value.entry.value, MappingNode) or not value.entry.value.value
    }


# Each scenario is a set of files under conf/; conf/main.yaml is the primary config.
SCENARIOS = {
    "self_last": {
        "base.yaml": "model:\n  width: 1\n  depth: 2\n",
        "main.yaml": "defaults: [base, _self_]\nmodel:\n  width: 9\n",
    },
    "self_first": {
        "base.yaml": "model:\n  width: 1\n  depth: 2\n",
        "main.yaml": "defaults: [_self_, base]\nmodel:\n  width: 9\n",
    },
    "implicit_self_is_last": {
        "base.yaml": "model:\n  width: 1\n",
        "main.yaml": "defaults: [base]\nmodel:\n  width: 9\n",
    },
    "group": {
        "model/small.yaml": "width: 32\n",
        "main.yaml": "defaults:\n  - model: small\n",
    },
    "package_override": {
        "model/small.yaml": "width: 32\n",
        "main.yaml": "defaults:\n  - model@network: small\n",
    },
    "header_package": {
        "model/small.yaml": "# @package network.inner\nwidth: 32\n",
        "main.yaml": "defaults:\n  - model: small\n",
    },
    "override_beats_header": {
        "model/small.yaml": "# @package ignored\nwidth: 32\n",
        "main.yaml": "defaults:\n  - model@network: small\n",
    },
    "header_global": {
        "model/small.yaml": "# @package _global_\nwidth: 32\n",
        "main.yaml": "defaults:\n  - model: small\n",
    },
    "header_global_dotted": {
        "model/small.yaml": "# @package _global_.top\nwidth: 32\n",
        "main.yaml": "defaults:\n  - model: small\n",
    },
    "header_after_content_is_ignored": {
        "model/small.yaml": "width: 32\n# @package _global_\n",
        "main.yaml": "defaults:\n  - model: small\n",
    },
    "nested_group": {
        "server/apache.yaml": "defaults:\n  - db: mysql\n  - _self_\nport: 80\n",
        "server/db/mysql.yaml": "host: localhost\n",
        "main.yaml": "defaults:\n  - server: apache\n",
    },
    "nested_group_under_package_override": {
        "server/apache.yaml": "defaults:\n  - db: mysql\n  - _self_\nport: 80\n",
        "server/db/mysql.yaml": "host: localhost\n",
        "main.yaml": "defaults:\n  - server@web: apache\n",
    },
    "nested_here": {
        "server/apache.yaml": "defaults:\n  - db@_here_: mysql\n  - _self_\nport: 80\n",
        "server/db/mysql.yaml": "host: localhost\n",
        "main.yaml": "defaults:\n  - server: apache\n",
    },
    "nested_global_override": {
        "server/apache.yaml": "defaults:\n  - db@_global_.database: mysql\nport: 80\n",
        "server/db/mysql.yaml": "host: localhost\n",
        "main.yaml": "defaults:\n  - server: apache\n",
    },
    "nested_absolute_group": {
        "server/apache.yaml": "defaults:\n  - /db: postgres\nport: 80\n",
        "db/postgres.yaml": "host: remote\n",
        "main.yaml": "defaults:\n  - server: apache\n",
    },
    "nested_absolute_group_under_package_override": {
        "server/apache.yaml": "defaults:\n  - /db: postgres\nport: 80\n",
        "db/postgres.yaml": "host: remote\n",
        "main.yaml": "defaults:\n  - server@web: apache\n",
    },
    "nested_header_is_absolute": {
        "server/apache.yaml": "defaults:\n  - db: mysql\nport: 80\n",
        "server/db/mysql.yaml": "# @package foo.bar\nhost: localhost\n",
        "main.yaml": "defaults:\n  - server@web: apache\n",
    },
    "nested_header_global_dotted": {
        "server/apache.yaml": "defaults:\n  - db: mysql\nport: 80\n",
        "server/db/mysql.yaml": "# @package _global_.z\nhost: localhost\n",
        "main.yaml": "defaults:\n  - server: apache\n",
    },
    "header_group_keyword_is_literal": {
        "model/small.yaml": "# @package _group_\nwidth: 32\n",
        "main.yaml": "defaults:\n  - model: small\n",
    },
    "override_group_keyword": {
        "server/apache.yaml": "defaults:\n  - db@_group_.x: mysql\nport: 80\n",
        "server/db/mysql.yaml": "host: localhost\n",
        "main.yaml": "defaults:\n  - server: apache\n",
    },
    "nested_subgroup_key": {
        "server/apache.yaml": "defaults:\n  - db/engine: inno\nport: 80\n",
        "server/db/engine/inno.yaml": "e: 1\n",
        "main.yaml": "defaults:\n  - server: apache\n",
    },
    "nested_slash_path": {
        "server/apache.yaml": "defaults:\n  - db/mysql\nport: 80\n",
        "server/db/mysql.yaml": "host: localhost\n",
        "main.yaml": "defaults:\n  - server: apache\n",
    },
    "nested_plain_config": {
        "server/apache.yaml": "defaults:\n  - common\nport: 80\n",
        "server/common.yaml": "timeout: 5\n",
        "main.yaml": "defaults:\n  - server: apache\n",
    },
    "primary_header_wraps_everything": {
        "server/apache.yaml": "defaults:\n  - db: mysql\nport: 80\n",
        "server/db/mysql.yaml": "host: localhost\n",
        "main.yaml": "# @package foo\ndefaults:\n  - server: apache\nk: 1\n",
    },
    "multiple_options": {
        "feature/a.yaml": "a: 1\n",
        "feature/b.yaml": "b: 2\n",
        "main.yaml": "defaults:\n  - feature: [a, b]\n",
    },
    "scalar_replaces_mapping": {
        "base.yaml": "model:\n  width: 32\n  bias: true\n",
        "middle.yaml": "defaults: [base]\nmodel: null\n",
        "main.yaml": "defaults: [middle]\n",
    },
    "mapping_merge_across_files": {
        "base.yaml": "model:\n  width: 32\n  opt:\n    lr: 1\n",
        "main.yaml": "defaults: [base, _self_]\nmodel:\n  opt:\n    momentum: 0.9\n",
    },
}


@pytest.mark.filterwarnings("ignore:In 'main'. Defaults list is missing `_self_`")
@pytest.mark.parametrize("files", SCENARIOS.values(), ids=SCENARIOS.keys())
def test_static_composition_matches_hydra(project, files):
    root, write = project
    for name, text in files.items():
        write(f"conf/{name}", text)
    expected = hydra_leaves(root / "conf")
    actual = hydradex_leaves(root, root / "conf/main.yaml")
    assert set(actual) == set(expected)
    for path, value in expected.items():
        # Compare YAML scalars loosely: HydraDex keeps source text, Hydra types it.
        if value is None:
            assert actual[path] in ("null", "", "~")
        else:
            assert actual[path] == (str(value).lower() if isinstance(value, bool) else str(value))


def test_inherited_example_target_and_config_keys():
    path = REPO / "examples/conf/experiment.yaml"
    with HydraDex([REPO]) as engine:
        items = completions(
            engine, path, "defaults: [config, _self_]\nmodel:\n  hidden_dim: 128\n  |"
        )
        assert {"input_dim", "activation", "dropout"} <= items.keys()
        assert "hidden_dim" not in items
        assert items["dropout"].text_edit.new_text == "dropout: "
        items = completions(
            engine, path, "defaults: [config, _self_]\ntraining:\n  epochs: 20\n  ba|"
        )
        assert set(items) == {"batch_size"}
        assert "config.yaml" in items["batch_size"].detail


def test_inherited_function_parameters():
    path = REPO / "examples/conf/experiment.yaml"
    with HydraDex([REPO]) as engine:
        items = completions(
            engine, path, "defaults: [config, _self_]\noptimizer:\n  lr: 0.02\n  we|"
        )
        assert set(items) == {"weight_decay"}


def test_local_target_overrides_inherited_target():
    path = REPO / "examples/conf/experiment.yaml"
    with HydraDex([REPO]) as engine:
        items = completions(
            engine,
            path,
            "defaults: [config]\nmodel:\n  _target_: examples.components.Dataset\n  se|",
        )
        assert set(items) == {"seed"}


@pytest.mark.parametrize(
    ("group", "directive", "parent"),
    [
        ("model", "", "model"),
        ("model@network", "", "network"),
        ("model", "# @package network\n", "network"),
        ("model@network", "# @package ignored\n", "network"),
        ("model@_global_", "", ""),
    ],
)
def test_completion_follows_package_placement(project, group, directive, parent):
    root, write = project
    write("conf/model/small.yaml", directive + "width: 32\nbias: true\n")
    source = write("conf/main.yaml", f"defaults:\n  - {group}: small\n  - _self_\n")
    with HydraDex([root]) as engine:
        text = source.read_text() + (f"{parent}:\n  wi|" if parent else "wi|")
        assert set(completions(engine, source, text)) == {"width"}


def test_nested_defaults_overlays_cycles_and_missing_files(project):
    root, write = project
    base = write("conf/base.yaml", "training:\n  epochs: 5\n")
    write("conf/middle.yaml", "defaults: [base, main, missing]\n")
    source = write("conf/main.yaml", "defaults: [middle]\n")
    with HydraDex([root]) as engine:
        text = source.read_text() + "training:\n  |"
        assert "epochs" in completions(engine, source, text)
        engine.update(base.as_uri(), "training:\n  steps: 100\n")
        items = completions(engine, source, text)
        assert "steps" in items and "epochs" not in items
        engine.close(base.as_uri())
        assert "epochs" in completions(engine, source, text)


def test_completion_in_a_broken_file_still_uses_defaults(project):
    root, write = project
    write("conf/base.yaml", "training:\n  epochs: 5\n")
    source = write("conf/main.yaml", "")
    with HydraDex([root]) as engine:
        text = "defaults: [base]\nbroken: [\ntraining:\n  ep|"
        # The unrelated syntax error prevents any composition; nothing is invented.
        assert completions(engine, source, text) == {}
        text = "defaults: [base]\ntraining:\n  ep|\nlater: [1, 2"
        assert set(completions(engine, source, text)) <= {"epochs"}


@pytest.mark.parametrize("experiment", [False, True])
def test_runnable_example_instantiates_and_overrides(tmp_path, experiment):
    command = [sys.executable, "-m", "examples.train"]
    if experiment:
        command += ["--config-name", "experiment", "model.hidden_dim=256", "training.batch_size=32"]
    command.append(f"hydra.run.dir={tmp_path}")
    result = subprocess.run(
        command, cwd=REPO, capture_output=True, text=True, check=True, timeout=60
    )
    output = json.loads(result.stdout)
    assert output["model"]["hidden_dim"] == (256 if experiment else 64)
    assert output["model"]["input_dim"] == output["dataset"]["feature_dim"] == 8
    assert output["dataset"]["samples"] == (1000 if experiment else 100)
    assert output["optimizer"]["lr"] == (0.0003 if experiment else 0.001)
    assert output["training"]["epochs"] == (20 if experiment else 5)
    assert output["training"]["batch_size"] == (32 if experiment else 16)
