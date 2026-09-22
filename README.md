# HydraDex

Hydra configuration intelligence as a **Python library and standalone language
server**, with **LazyVim / Neovim** as the primary editor target.

HydraDex connects YAML `_target_` values to Python code and indexes Hydra
configuration references. Version 0.2 is a complete rewrite of the original
VS Code extension. Python analysis runs through [ty](https://github.com/astral-sh/ty),
using its standard LSP interface.

## Features

- Go to Python definitions from `_target_` values, including re-exports and methods.
- Complete Python module/class/function names, including partially typed targets.
- Complete constructor and function keyword parameters; omit existing YAML keys,
  positional-only arguments, and variadic parameter names.
- Complete override keys and Python parameters inherited through literal defaults
  references, including nested config groups and `_self_` precedence.
- Hover for Python documentation and resolved defaults-list filenames.
- Navigate defaults entries, including groups, `override`, `optional`, package
  suffixes, option lists, absolute groups, and `.yaml` / `.yml` files.
- Navigate `${path.to.key}` interpolations across workspace YAML files, with
  ranked matches, workspace isolation, `# @package`, and relative `${.key}` /
  `${..key}` references.
- Report YAML syntax errors and unresolved static Python targets.
- Track unsaved YAML buffers, incremental edits, file changes, and workspace folders.

```yaml
defaults:
  - model: small              # gd opens model/small.yaml

model:
  _target_: my_app.Model      # gd opens Python; K shows documentation
  width: 128                 # completion offers remaining constructor parameters

training:
  model_width: ${model.width} # gd jumps to the YAML key
```

## Install

Requires Python **3.11+**. From a checkout:

```sh
uv tool install .
hydradex --version
```

Or install into your project's environment:

```sh
pip install -e .
```

`ty` is included as the Python analysis backend.

The server starts with `hydradex --stdio` or `python -m hydradex`. Logging goes
to stderr, leaving stdout exclusively for LSP messages.

## Runnable examples

[`examples/conf/`](examples/conf/) contains model, dataset, and optimizer configs
with real Python targets, plus an experiment that overrides the base values.

```sh
uv run --extra examples python -m examples.train
uv run --extra examples python -m examples.train --config-name experiment
```

In `examples/conf/experiment.yaml`, complete `dro` under `model:` to get `dropout`
from the inherited Python constructor, or `ba` under `training:` to get
`batch_size` from the base config. See [the walkthrough](examples/README.md).

## LazyVim setup

Requires Neovim **0.11+**. Copy
[`examples/lazyvim.lua`](examples/lazyvim.lua) to
`~/.config/nvim/lua/plugins/hydradex.lua`.

Ensure `hydradex` is on Neovim's `PATH`. If using a project-local installation,
change `cmd` to the absolute path of that environment's `hydradex` executable.
Open a YAML file in a project with `pyproject.toml` or `.git` and check `:LspInfo`.

LazyVim's usual `gd`, `K`, completion, and diagnostic navigation work through LSP.
HydraDex can run alongside `yamlls` for YAML schemas and formatting. Enable
LazyVim's YAML language extra if you want those additional features.

The Python backend discovers conventional project environments. If HydraDex
is installed separately from your application, set `pythonPath` to the project's
Python executable or virtual environment. `extraPaths` supports additional source
directories; project-root and existing `src/` directories are included automatically.

To manually refresh the index:

```lua
for _, client in ipairs(vim.lsp.get_clients({ bufnr = 0, name = "hydradex" })) do
  client:request("workspace/executeCommand", { command = "hydradex.refreshIndex" })
end
```

### Plain Neovim

```lua
vim.lsp.config("hydradex", {
  cmd = { "hydradex", "--stdio" },
  filetypes = { "yaml" },
  root_markers = { "pyproject.toml", ".git" },
})
vim.lsp.enable("hydradex")
```

## Python library

```python
from pathlib import Path
from lsprotocol.types import Position
from hydradex import HydraDex

root = Path("/path/to/project")
uri = (root / "conf" / "model.yaml").as_uri()

with HydraDex([root]) as hydra:
    hydra.update(uri, "_target_: my_app.Model\n", version=1)
    definitions = hydra.definitions(uri, Position(line=0, character=15))
    diagnostics = hydra.diagnostics(uri)
    hover = hydra.hover(uri, Position(line=0, character=15))
```

Results use `lsprotocol` types. All positions are zero-based UTF-16, matching the
server's advertised position encoding. Library instances are synchronous and
single-thread-owned; the LSP adapter runs analysis on a serialized worker.

- `update(uri, text, version=None)` opens/replaces an in-memory YAML buffer.
- `close(uri)` discards that buffer and restores its disk contents in the index.
- `definitions(uri, position)`, `completions(...)`, `hover(...)`, and
  `diagnostics(uri)` expose editor-independent intelligence.
- `refresh()` rescans files while retaining buffer overlays.
- `shutdown()` releases backend processes; subsequent requests can restart them.
- Use the context manager to guarantee process cleanup.

Backend failures raise `hydradex.python.BackendError`. The language server reports
backend availability separately from unresolved-target diagnostics.

## Configuration

Pass settings as `initializationOptions` (Neovim `init_options`) or under
`settings.hydradex`. `workspace/didChangeConfiguration` replaces the configuration
and rebuilds analysis while preserving unsaved YAML buffers.

| LSP setting | Library setting | Default |
| --- | --- | --- |
| `pythonPath` | `python_path` | Automatic environment discovery |
| `extraPaths` | `extra_paths` | `[]` |
| `configRoots` | `config_roots` | `[]` |
| `backendCommand` | `backend_command` | `ty` executable + `server` |
| `backendTimeout` | `backend_timeout` | `15` seconds per backend operation |
| `matchFilter` | `match_filter` | `"top matches only"` |
| `isolateWorkspaceFolders` | `isolate_workspace_folders` | `true` |
| `excludePatterns` | `exclude_patterns` | `.git/`, `.venv/`, `venv/`, `node_modules/`, `__pycache__/`, `dist/`, `build/` |

Paths are relative to each workspace root. Exclusions use gitignore-style patterns
through `pathspec`. `matchFilter` also accepts `"all"` and `"perfect matches only"`.
Matches are ranked by the longest matching path suffix and deduplicated by key
location. Workspace isolation happens before ranking.

Set `configRoots` for nonstandard Hydra config directories. Relative defaults
search the source directory and its ancestors within the workspace, followed by
configured config roots. Absolute defaults search configured roots, or conventional
`conf` / `config` / `configs` ancestors, falling back to the workspace root.

## Dependencies

- **ty**, maintained by Astral, is the Python analysis backend and the
  development type checker. Its keyword-argument completions preserve Python
  calling conventions directly.
- **Pygls / lsprotocol** handle both sides of LSP, including message framing,
  document synchronization, and protocol types.
- **PyYAML** provides syntax trees and source ranges without constructing YAML
  application objects; **pathspec** provides exclusion matching.

Python helper documents are opened in memory through LSP. No shadow files are
written to the project and target modules are not imported or executed by HydraDex.

### Static-analysis boundaries

HydraDex indexes possible definitions; it does not execute Hydra's composition
engine. Override completion follows literal defaults references, package directives
and overrides, `_self_` ordering, and mapping merges. Full defaults-group selection
overrides (`override group: option`), runtime resolver calls, dynamic `_target_`
interpolations, and Python-defined ConfigStore entries are not evaluated. Navigation
results can include multiple config alternatives. Plain node interpolations inside mapping
values are supported; arbitrary OmegaConf resolver grammar is not interpreted.

Parameter completion repairs the current block-style YAML key while typing. Python
resolution remains subject to ty's support for dynamic code.
Filesystem watching uses the editor's LSP watcher support; `refreshIndex` is available
for clients without it. Python files changed on disk are picked up by watcher-driven
backend restarts; unsaved Python buffers in another LSP client are not shared.

## Development and tests

```sh
uv sync --locked
uv run pytest --cov=hydradex --cov-report=term-missing
uv run ruff check .
uv run ruff format --check .
uv run ty check src
uv build
```

Tests cover YAML syntax and source positions, interpolation ranking and isolation,
defaults navigation, buffer overlays, actual ty analysis, process lifecycle,
and a real stdio LSP session. A headless Neovim test loads the supplied LazyVim
server configuration and exercises attachment, navigation, and completion when
Neovim 0.11+ is available. CI runs the suite on Linux, macOS, and Windows and runs
the Neovim integration separately on Linux.

HydraDex is MIT-licensed and independent of the Hydra project.
