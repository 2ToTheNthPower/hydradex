# Runnable Hydra configs

From the repository root:

```sh
uv run --extra examples python -m examples.train
uv run --extra examples python -m examples.train --config-name experiment
uv run --extra examples python -m examples.train --config-name experiment model.hidden_dim=256 training.epochs=30
```

The runner uses real Hydra composition and `hydra.utils.instantiate` to create
`Model`, `Dataset`, and the result of `make_optimizer` from `components.py`.
It prints the instantiated settings as JSON. Hydra creates its usual run output
directory; no ML framework is required.

## Try autocomplete in LazyVim

1. Open `examples/conf/model/small.yaml`. Use `gd` or `K` on `_target_` for the
   Python class, or complete the dotted target name.
2. Open `examples/conf/experiment.yaml`, which inherits `config.yaml`.
3. Add an indented line under `model:` and type `dro`. Completion offers
   `dropout: ` from the inherited Python target's constructor. Set it to `0.2`.
4. Add an indented line under `training:` and type `ba`. Completion offers
   `batch_size: ` inherited from `config.yaml`, even though this mapping has no
   Python target. Set it to `32`.
5. Run the experiment command again to see your overrides in the output.

Use LazyVim's completion menu (or `<C-Space>` to open it manually). Keys already
present in the current mapping are filtered out; inherited keys remain available
so you can override their values. Unsaved edits in referenced YAML buffers are
also used by completion.

`_self_` comes last so the experiment's values take precedence. Move it before
`config` to give the imported config precedence instead. The interpolation in
`model/small.yaml` takes `input_dim` from the selected dataset's `feature_dim`.
