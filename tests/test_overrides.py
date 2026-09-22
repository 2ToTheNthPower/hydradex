"""Defaults-aware editing checked against Hydra's real composition semantics."""

import json
import subprocess
import sys
from pathlib import Path

import pytest
from hydra import compose as hydra_compose
from hydra import initialize_config_dir
from lsprotocol import types as lsp

from hydradex import HydraDex
from hydradex.composition import compose

REPO = Path(__file__).resolve().parents[1]


def completions(engine, path, text):
    """Use | as the cursor, without saving the incomplete YAML file."""
    before, after = text.split("|")
    position = lsp.Position(before.count("\n"), len(before.split("\n")[-1]))
    engine.update(path.as_uri(), before + after)
    return {item.label: item for item in engine.completions(path.as_uri(), position)}


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


@pytest.mark.parametrize("self_first", [False, True])
def test_self_order_matches_hydra(project, self_first):
    root, write = project
    write("base.yaml", "model:\n  _target_: examples.components.Model\n  hidden_dim: 32\n")
    references = "[_self_, base]" if self_first else "[base, _self_]"
    source = write("main.yaml", f"defaults: {references}\nmodel:\n  hidden_dim: 128\n")
    with initialize_config_dir(version_base=None, config_dir=str(root)):
        expected = hydra_compose(config_name="main")
    with HydraDex([root]) as engine:
        entries = compose(engine.workspace, engine.document(source.as_uri()))
        assert int(entries[("model", "hidden_dim")].entry.value.value) == expected.model.hidden_dim


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
def test_group_and_package_placement_matches_hydra(project, group, directive, parent):
    root, write = project
    write("conf/model/small.yaml", directive + "width: 32\nbias: true\n")
    source = write("conf/main.yaml", f"defaults:\n  - {group}: small\n  - _self_\n")
    with initialize_config_dir(version_base=None, config_dir=str(root / "conf")):
        expected = hydra_compose(config_name="main")
    with HydraDex([root]) as engine:
        text = source.read_text() + (f"{parent}:\n  wi|" if parent else "wi|")
        items = completions(engine, source, text)
        assert set(items) == {"width"}
        assert (expected[parent].width if parent else expected.width) == 32


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


def test_scalar_replacement_clears_inherited_keys(project):
    root, write = project
    write("base.yaml", "model:\n  width: 32\n  bias: true\n")
    write("middle.yaml", "defaults: [base]\nmodel: null\n")
    source = write("main.yaml", "defaults: [middle]\n")
    with HydraDex([root]) as engine:
        assert not completions(engine, source, source.read_text() + "model:\n  |")


def test_local_target_overrides_inherited_target():
    path = REPO / "examples/conf/experiment.yaml"
    with HydraDex([REPO]) as engine:
        items = completions(
            engine,
            path,
            "defaults: [config]\nmodel:\n  _target_: examples.components.Dataset\n  se|",
        )
        assert set(items) == {"seed"}


@pytest.mark.parametrize("experiment", [False, True])
def test_runnable_example_instantiates_and_overrides(tmp_path, experiment):
    command = [sys.executable, "-m", "examples.train"]
    if experiment:
        command += ["--config-name", "experiment", "model.hidden_dim=256", "training.batch_size=32"]
    command.append(f"hydra.run.dir={tmp_path}")
    result = subprocess.run(
        command, cwd=REPO, capture_output=True, text=True, check=True, timeout=20
    )
    output = json.loads(result.stdout)
    assert output["model"]["hidden_dim"] == (256 if experiment else 64)
    assert output["model"]["input_dim"] == output["dataset"]["feature_dim"] == 8
    assert output["dataset"]["samples"] == (1000 if experiment else 100)
    assert output["optimizer"]["lr"] == (0.0003 if experiment else 0.001)
    assert output["training"]["epochs"] == (20 if experiment else 5)
    assert output["training"]["batch_size"] == (32 if experiment else 16)
